"""Channels that show the same camera, or share a name, on one NVR; and
cameras recorded by more than one NVR.

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


# ---------- across NVRs ----------

# Addresses an NVR's built-in PoE switch gives its cameras repeat on every
# NVR, so equal addresses there are different cameras: never compared.
UNCOMPARABLE_PREFIXES = ("192.168.254.", "169.254.")


def find_fleet_duplicates(nvrs) -> list[dict]:
    """Cameras (same IP and input) recorded by more than one NVR.

    nvrs: iterable of (nvr_id, channel rows with channel_id, name, camera_ip,
    source_input). Returns [{'cameras': [{'nvr', 'channel_id', 'name'}]}].
    """
    seen: dict[tuple, list[dict]] = {}
    for nvr_id, channels in nvrs:
        for ch in channels or []:
            if not isinstance(ch, dict):
                continue
            cid = str(ch.get("channel_id") or "")
            ip = str(ch.get("camera_ip") or "").strip()
            if not cid or ip in NO_ADDRESS or ip.startswith(UNCOMPARABLE_PREFIXES):
                continue
            source = ch.get("source_input") if isinstance(ch.get("source_input"), int) else None
            seen.setdefault((ip, source), []).append(
                {"nvr": str(nvr_id), "channel_id": cid, "name": " ".join(str(ch.get("name") or "").split())[:80]})
    groups = []
    for cameras in seen.values():
        if len({c["nvr"] for c in cameras}) > 1:
            cameras.sort(key=lambda c: (c["nvr"], _order(c["channel_id"])))
            groups.append({"cameras": cameras})
    groups.sort(key=lambda g: (g["cameras"][0]["nvr"], _order(g["cameras"][0]["channel_id"])))
    return groups[:MAX_GROUPS]


def fleet_notes(groups: list[dict]) -> dict[tuple[str, str], list[str]]:
    """Per (nvr, channel): 'Also recorded by nvr-04 ch19' (+ its name there if different)."""
    notes: dict[tuple[str, str], list[str]] = {}
    for group in groups or []:
        cameras = group.get("cameras") or []
        for camera in cameras:
            others = []
            for other in cameras:
                if other is camera or other["nvr"] == camera["nvr"]:
                    continue
                named = (f" (named “{other['name'][:40]}” there)"
                         if other["name"] and other["name"].casefold() != camera["name"].casefold() else "")
                others.append(f"{other['nvr']} ch{other['channel_id']}{named}")
            if others:
                notes.setdefault((camera["nvr"], camera["channel_id"]), []).append(
                    "Also recorded by " + ", ".join(others[:4]))
    return notes


def fleet_info(groups: list[dict], nvr_id: str) -> list[str]:
    """Information line for one NVR (never a warning: often a central NVR on purpose)."""
    parts = []
    for group in groups or []:
        cameras = group.get("cameras") or []
        mine = [c for c in cameras if c["nvr"] == nvr_id]
        others = [f"{c['nvr']} ch{c['channel_id']}" for c in cameras if c["nvr"] != nvr_id]
        if mine and others:
            parts.append(f"ch{mine[0]['channel_id']} = " + ", ".join(others[:4]))
    if not parts:
        return []
    more = f" and {len(parts) - 5} more" if len(parts) > 5 else ""
    return [f"Cameras also recorded by another NVR: {'; '.join(parts[:5])}{more}. "
            "Fine if intended; it uses disk space on both."]


def fleet_text(groups: list[dict]) -> list[str]:
    """One line per camera recorded by several NVRs (no IP addresses)."""
    return [" = ".join(f"{c['nvr']} ch{c['channel_id']}" for c in g.get("cameras") or []) for g in groups or []]
