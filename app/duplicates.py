"""Channels that show the same camera, or share a name, on one NVR.

Pure functions over the channel list a report already holds; no NVR request.
Two channels with the same camera IP and camera input record one picture
twice, so the camera meant for one of them is probably not connected at all.
Multi-lens cameras and encoders serve several inputs from one IP address;
they differ in srcInputPort and are not flagged. Two channels with one name
make the dashboard, email, alerts and camera search ambiguous.

Only a channel list that was really retrieved is checked (never the
status-list fallback, which has no names or IPs). Messages never contain IP
addresses: they also reach Telegram viewers, who may not see IPs.
"""
from __future__ import annotations

MAX_GROUPS = 50
NO_ADDRESS = {"", "0.0.0.0", "::"}


def _order(channel_id: str):
    return (0, int(channel_id)) if channel_id.isdigit() else (1, channel_id)


def find_duplicates(channels: list[dict] | None) -> dict:
    """{'status', 'same_camera': [{'channels': [...]}], 'same_name': [{'name', 'channels'}]}."""
    if channels is None:
        return {"status": "NOT_CHECKED", "same_camera": [], "same_name": []}
    cameras: dict[tuple, list[str]] = {}
    names: dict[str, tuple[str, list[str]]] = {}
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("channel_id") or "")
        ip = str(ch.get("camera_ip") or "").strip()
        if cid and ip not in NO_ADDRESS:
            source = ch.get("source_input") if isinstance(ch.get("source_input"), int) else None
            cameras.setdefault((ip, source), []).append(cid)
        name = " ".join(str(ch.get("name") or "").split())
        if cid and name:
            names.setdefault(name.casefold(), (name, []))[1].append(cid)
    same_camera = [{"channels": sorted(ids, key=_order)} for ids in cameras.values() if len(ids) > 1]
    same_name = [{"name": name[:80], "channels": sorted(ids, key=_order)}
                 for name, ids in names.values() if len(ids) > 1]
    same_camera.sort(key=lambda group: _order(group["channels"][0]))
    same_name.sort(key=lambda group: _order(group["channels"][0]))
    return {"status": "CHECKED", "same_camera": same_camera[:MAX_GROUPS], "same_name": same_name[:MAX_GROUPS]}


def warnings(duplicates: dict | None) -> list[str]:
    """Overall-status warning: one camera on several channels (no IP addresses)."""
    duplicates = duplicates if isinstance(duplicates, dict) else {}
    cameras = [group for group in duplicates.get("same_camera") or [] if isinstance(group, dict)]
    if not cameras:
        return []
    listed = "; ".join(" = ".join(f"ch{c}" for c in group.get("channels", [])) for group in cameras[:5])
    more = f" and {len(cameras) - 5} more" if len(cameras) > 5 else ""
    return [f"Same camera on more than one channel: {listed}{more}. "
            "It is recorded twice; check whether another camera is missing."]


def info(duplicates: dict | None) -> list[str]:
    """Information only, never a warning: several cameras often share one
    location name ("Warehouse 3" ch27-32), so this must not change NVR status."""
    duplicates = duplicates if isinstance(duplicates, dict) else {}
    names = [group for group in duplicates.get("same_name") or [] if isinstance(group, dict)]
    if not names:
        return []
    listed = "; ".join(f"“{str(group.get('name', ''))[:40]}” ("
                       + ", ".join(f"ch{c}" for c in group.get("channels", [])) + ")" for group in names[:5])
    more = f" and {len(names) - 5} more" if len(names) > 5 else ""
    return [f"Channel names used more than once: {listed}{more}. Unique names make alerts and searches clearer."]


def notes_by_channel(duplicates: dict | None) -> dict[str, list[str]]:
    """Per camera: 'Same camera as ch51', 'Same name as ch22'."""
    duplicates = duplicates if isinstance(duplicates, dict) else {}
    notes: dict[str, list[str]] = {}
    for key, text in (("same_camera", "Same camera as"), ("same_name", "Same name as")):
        for group in duplicates.get(key) or []:
            ids = [str(c) for c in (group.get("channels") or [])] if isinstance(group, dict) else []
            for cid in ids:
                others = [f"ch{other}" for other in ids if other != cid]
                if others:
                    notes.setdefault(cid, []).append(f"{text} {', '.join(others[:5])}")
    return notes
