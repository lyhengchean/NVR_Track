"""Camera list changes and NVR restarts, by comparing a report with earlier ones.

Pure functions over saved report data; no NVR requests. Only a channel list
that was actually retrieved (not the status-list fallback, not an unreachable
NVR) is compared, so an outage never looks like "cameras removed".
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

MAX_CHANGES = 100
RESTART_TOLERANCE = timedelta(minutes=5)


def recent_reports(report_dir: Path, nvr_id: str, limit: int = 24):
    """Saved reports of one NVR, newest first (unreadable ones skipped)."""
    count = 0
    for path in sorted(report_dir.glob(f"{nvr_id}_*.json"), reverse=True):
        if count >= limit:
            return
        try:
            with path.open("r", encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(report, dict) or (report.get("nvr") or {}).get("id") != nvr_id:
            continue
        count += 1
        yield report


def retrieved_inventory(report: dict) -> list[dict] | None:
    inventory = report.get("channel_inventory") or {}
    if inventory.get("status") != "CHANNELS_RETRIEVED" or \
            inventory.get("channel_source", "CHANNEL_INVENTORY") != "CHANNEL_INVENTORY":
        return None
    channels = inventory.get("channels")
    return [c for c in channels if isinstance(c, dict)] if isinstance(channels, list) else None


def compare_inventory(previous: list[dict], current: list[dict]) -> list[dict]:
    """added / removed / renamed / ip_changed, one entry per change."""
    before = {str(c.get("channel_id")): c for c in previous}
    after = {str(c.get("channel_id")): c for c in current}

    def order(cid: str):
        return (0, int(cid)) if cid.isdigit() else (1, cid)
    changes = []
    for cid in sorted(after.keys() - before.keys(), key=order):
        changes.append({"type": "added", "channel_id": cid, "new": after[cid].get("name") or ""})
    for cid in sorted(before.keys() - after.keys(), key=order):
        changes.append({"type": "removed", "channel_id": cid, "old": before[cid].get("name") or ""})
    for cid in sorted(before.keys() & after.keys(), key=order):
        old, new = before[cid], after[cid]
        if old.get("name") and new.get("name") and old["name"] != new["name"]:
            changes.append({"type": "renamed", "channel_id": cid, "old": old["name"], "new": new["name"]})
        if old.get("camera_ip") and new.get("camera_ip") and old["camera_ip"] != new["camera_ip"]:
            changes.append({"type": "ip_changed", "channel_id": cid,
                            "old": old["camera_ip"], "new": new["camera_ip"]})
    return changes[:MAX_CHANGES]


def inventory_changes(previous_reports, current_channels: list[dict] | None) -> dict:
    """Compare with the newest earlier report whose channel list was retrieved."""
    if current_channels is None:
        return {"status": "NOT_COMPARED", "compared_with": None, "changes": []}
    for report in previous_reports:
        channels = retrieved_inventory(report)
        if channels is None:
            continue
        return {"status": "COMPARED", "compared_with": report.get("check_completed_at"),
                "changes": compare_inventory(channels, current_channels)}
    return {"status": "FIRST_INVENTORY", "compared_with": None, "changes": []}


def restart_info(uptime_seconds: int | None, now: datetime, previous_check: str | None) -> dict:
    """Did the NVR boot after the previous report? (boot time = now - uptime)."""
    if not isinstance(uptime_seconds, int) or now.tzinfo is None:
        return {"detected": None, "boot_time": None}
    boot = now - timedelta(seconds=uptime_seconds)
    detected = None
    try:
        previous = datetime.fromisoformat(previous_check) if previous_check else None
        if previous is not None and previous.tzinfo is not None:
            detected = boot > previous + RESTART_TOLERANCE
    except (TypeError, ValueError):
        detected = None
    return {"detected": detected, "boot_time": boot.replace(microsecond=0).isoformat()}
