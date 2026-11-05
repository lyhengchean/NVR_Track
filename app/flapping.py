"""Cameras that keep going offline and online again ("unstable connection").

Built only from the live state the NVR reports at each scan; nothing here
contacts an NVR. Each saved report keeps, per camera, when its live state
changed within the last NVR_FLAP_HOURS (``live_changes``) and its last known
state (``live_last``); the next report extends that list. When the previous
report has no such list (older version, or a failed run), the list is rebuilt
from the saved reports at hand. A drop-out between two scans is not seen, so
the count is a lower bound: it never claims more changes than were observed.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

LIVE = {"ONLINE", "OFFLINE"}
MAX_KEPT = 50


def settings(env: dict | None = None) -> dict:
    """NVR_FLAP_CHANGES (2-50, default 4) changes within NVR_FLAP_HOURS (1-168, default 24)."""
    env = os.environ if env is None else env

    def number(name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(str(env.get(name, default)).strip())
        except ValueError:
            return default
        return value if low <= value <= high else default
    return {"changes": number("NVR_FLAP_CHANGES", 4, 2, 50), "hours": number("NVR_FLAP_HOURS", 24, 1, 168)}


def _when(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else None


def _rows(report: object) -> dict[str, dict]:
    channels = ((report or {}).get("channel_inventory") or {}).get("channels") if isinstance(report, dict) else None
    return {str(ch.get("channel_id", "")): ch for ch in channels or [] if isinstance(ch, dict)}


def _rebuild(previous_reports: list[dict], cutoff: datetime) -> dict[str, list[str]]:
    """Changes per channel seen across saved reports (any order) since cutoff."""
    timeline: dict[str, list[tuple[datetime, str]]] = {}
    for report in previous_reports:
        moment = _when(report.get("check_completed_at")) if isinstance(report, dict) else None
        if moment is None:
            continue
        for cid, ch in _rows(report).items():
            if ch.get("reported_status") in LIVE:
                timeline.setdefault(cid, []).append((moment, ch["reported_status"]))
    changes = {}
    for cid, points in timeline.items():
        points.sort()
        changes[cid] = [later.isoformat() for (_, before), (later, after) in zip(points, points[1:])
                        if before != after and later >= cutoff]
    return changes


def track(channels: list[dict], previous_reports: list[dict], now: datetime, hours: int) -> None:
    """Set live_changes and live_last on each channel row (in place)."""
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    previous = next((r for r in previous_reports if _rows(r)), {})
    rows = _rows(previous)
    chained = any(isinstance(ch.get("live_changes"), list) for ch in rows.values())
    rebuilt = {} if chained else _rebuild(previous_reports, cutoff)
    for ch in channels:
        cid = str(ch.get("channel_id", ""))
        before = rows.get(cid) or {}
        if chained:
            kept = [t for t in before.get("live_changes") or []
                    if isinstance(t, str) and (_when(t) or cutoff - timedelta(seconds=1)) >= cutoff]
        else:
            kept = list(rebuilt.get(cid, []))
        last = before.get("live_last") if before.get("live_last") in LIVE else before.get("reported_status")
        live = ch.get("reported_status")
        if live in LIVE and last in LIVE and live != last:
            kept.append(now.isoformat())
        ch["live_changes"] = kept[-MAX_KEPT:]
        ch["live_last"] = live if live in LIVE else (last if last in LIVE else None)


def summary(channels: list[dict] | None, needed: int, hours: int) -> dict:
    """Cameras with at least `needed` live-state changes in the window."""
    if channels is None:
        return {"status": "NOT_CHECKED", "changes_needed": needed, "hours": hours, "cameras": []}
    cameras = []
    for ch in channels:
        changes = [t for t in ch.get("live_changes") or [] if isinstance(t, str)]
        if len(changes) >= needed:
            cameras.append({"channel_id": str(ch.get("channel_id", "")), "name": str(ch.get("name") or "")[:80],
                            "changes": len(changes), "last_change": changes[-1],
                            "reported_status": ch.get("reported_status")})
    cameras.sort(key=lambda c: int(c["channel_id"]) if c["channel_id"].isdigit() else 10**6)
    return {"status": "CHECKED", "changes_needed": needed, "hours": hours, "cameras": cameras}


def warning(flapping: dict | None) -> str | None:
    """NVR warning line, or None."""
    cameras = (flapping or {}).get("cameras") or []
    if not cameras:
        return None
    listed = ", ".join(f"ch{c['channel_id']}" for c in cameras[:8]) + (f" and {len(cameras) - 8} more"
                                                                         if len(cameras) > 8 else "")
    return (f"{len(cameras)} {'camera keeps' if len(cameras) == 1 else 'cameras keep'} going offline and online "
            f"({flapping['changes_needed']}+ changes in {flapping['hours']} h): {listed}. "
            "Check cable, PoE switch port or power.")
