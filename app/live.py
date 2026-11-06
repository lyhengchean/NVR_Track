"""Live status between full scans: camera online/offline, disk states and
NVR reachability, checked every NVR_LIVE_SECONDS (default 60) by the
nvr-live service for the NVRs in NVR_LIVE_NVR_IDS.

Each check is the same two read-only GETs every scan already makes
(/ISAPI/ContentMgmt/InputProxy/channels/status and /ISAPI/ContentMgmt/Storage/hdd).
The dashboard lays the newest live result over the latest saved report, so
the Telegram alerts "went offline", "back online", disk state changes and
"not reachable" follow within minutes instead of at the next 2-hourly scan.
Recording, picture and SMART checks still come only from the scans.

Honest status: a failed or partial live check never changes what is shown;
the last good live data stays, and alerts treat it as unknown (no false
"back online" or "OK again").
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.flapping import warning as flap_warning
from app.reporting import (OFFLINE_SUFFIX, STORAGE_CRITICAL, STORAGE_UNKNOWN, STORAGE_WARNING,
                           offline_finding, storage_findings)

ROOT = Path(__file__).resolve().parent.parent
LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")
SCHEMA = 1
LIVE = {"ONLINE", "OFFLINE"}
STATES = {"ONLINE", "OFFLINE", "UNKNOWN"}
STORAGE_STATES = {"HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"}
NETWORK_FAILURES = {"TIMEOUT", "CONNECTION_ERROR"}
DOWN_AFTER_FAILURES = 2        # Consecutive unreachable checks before "not reachable".
DEFAULT_SECONDS = 60
DOWN_REASON = "Not reachable by the live check"
_OFFLINE_LINE = re.compile(r"^\d+" + re.escape(OFFLINE_SUFFIX) + "$")


def live_dir() -> Path:
    return Path(os.environ.get("LIVE_DIR", str(ROOT / "state" / "live")))


def interval_seconds(env: dict | None = None) -> int:
    """NVR_LIVE_SECONDS, 30-1800 (default 60)."""
    env = os.environ if env is None else env
    try:
        value = int(str(env.get("NVR_LIVE_SECONDS", DEFAULT_SECONDS)).strip())
    except ValueError:
        return DEFAULT_SECONDS
    return value if 30 <= value <= 1800 else DEFAULT_SECONDS


def selected_ids(env: dict | None = None) -> set[str]:
    env = os.environ if env is None else env
    return {item.strip().lower() for item in env.get("NVR_LIVE_NVR_IDS", "").split(",") if item.strip()}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _when(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else None


# ---------- files ----------

def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".live",
                                     suffix=".tmp", delete=False) as handle:
        json.dump(data, handle)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def write_entry(entry: dict, directory: Path | None = None) -> None:
    _write_json((directory or live_dir()) / f"{entry['nvr']}.json", entry)


def read_entry(nvr_id: str, directory: Path | None = None) -> dict | None:
    path = (directory or live_dir()) / f"{nvr_id}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return entry if _valid(entry, nvr_id) else None


def write_service(nvr_ids: list[str], interval: int, directory: Path | None = None) -> None:
    """Heartbeat of the nvr-live service (also its Docker healthcheck)."""
    _write_json((directory or live_dir()) / "service.json",
                {"schema_version": SCHEMA, "updated_at": now_iso(), "nvrs": sorted(nvr_ids),
                 "interval_seconds": interval})


def _valid(entry: object, nvr_id: str) -> bool:
    return isinstance(entry, dict) and entry.get("schema_version") == SCHEMA and entry.get("nvr") == nvr_id


def read_all(directory: Path | None = None) -> tuple[dict, dict[str, dict]]:
    """(service heartbeat or {}, {nvr_id: newest entry})."""
    directory = directory or live_dir()
    try:
        service = json.loads((directory / "service.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        service = {}
    entries = {}
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        if path.name == "service.json" or path.is_symlink():
            continue
        entry = read_entry(path.stem, directory)
        if entry is not None:
            entries[path.stem] = entry
    valid = isinstance(service, dict) and service.get("schema_version") == SCHEMA
    return (service if valid else {}), entries


def _interval(item: dict | None) -> int:
    value = (item or {}).get("interval_seconds")
    return value if type(value) is int and 30 <= value <= 1800 else DEFAULT_SECONDS


def service_alive(service: dict | None, now: datetime | None = None) -> bool:
    """Has the nvr-live service written its heartbeat recently?"""
    updated = _when((service or {}).get("updated_at"))
    return updated is not None and (now or datetime.now(timezone.utc)) - updated <= timedelta(
        seconds=max(3 * _interval(service), 300))


def fresh(entry: dict, now: datetime | None = None) -> bool:
    """Was the NVR checked within the last few intervals?"""
    checked = _when(entry.get("checked_at"))
    return checked is not None and (now or datetime.now(timezone.utc)) - checked <= timedelta(
        seconds=max(3 * _interval(entry), 300))


# ---------- one check (scripts/live_status.py) ----------

def next_entry(previous: dict | None, nvr_id: str, interval: int, channel_result, storage_result,
               writing_result=None, now: datetime | None = None, flap_hours: int = 24) -> dict:
    """The new entry after one check.

    channel_result: app.isapi.ChannelStatusResult; storage_result:
    app.storage.StorageCheck (strict); writing_result: the same disks
    classified as "being overwritten" (used when the latest scan saw footage).
    A failed part keeps its last good data and is marked not ok.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    previous = previous if _valid(previous, nvr_id) else {}
    channels_ok = channel_result.status in {"CHANNEL_STATUS_RETRIEVED", "CHANNEL_STATUS_INCOMPLETE"} \
        and bool(channel_result.channels)
    storage_ok = bool(storage_result.disks)  # Disk states read (status may be UNKNOWN: a disk in a new state).
    storage_network = (storage_result.status == "UNKNOWN" and storage_result.http_status is None
                       and not storage_result.disks and "Network" in storage_result.message)
    if channels_ok or storage_ok:
        reachable, error = True, None
    elif channel_result.status in NETWORK_FAILURES and storage_network:
        reachable, error = False, channel_result.status
    else:
        reachable, error = None, str(channel_result.status)[:40]  # Answered, but not usable (e.g. login).
    failures = (int(previous.get("failures") or 0) + 1) if reachable is False else 0
    down_since = (previous.get("down_since") or now.isoformat()) if reachable is False else None
    entry = {
        "schema_version": SCHEMA, "nvr": nvr_id, "interval_seconds": interval,
        "checked_at": now.isoformat(), "reachable": reachable, "error": error,
        "failures": failures, "down_since": down_since,
        "channels_ok": channels_ok, "storage_ok": storage_ok,
        "channels_at": previous.get("channels_at"), "channels": previous.get("channels") or {},
        "storage_at": previous.get("storage_at"), "storage": previous.get("storage"),
    }
    if channels_ok:
        entry["channels_at"] = now.isoformat()
        entry["channels"] = {str(c.channel_id): ("ONLINE" if c.online is True else
                                                 "OFFLINE" if c.online is False else "UNKNOWN")
                             for c in channel_result.channels}
    if storage_ok:
        writing = writing_result if writing_result is not None else storage_result
        entry["storage_at"] = now.isoformat()
        entry["storage"] = {
            "status": storage_result.status, "message": storage_result.message,
            "status_writing": writing.status, "message_writing": writing.message,
            "disks": [{"id": d.id, "name": d.name, "type": d.disk_type, "reported_status": d.status,
                       "capacity_mb": d.capacity_mb, "free_space_mb": d.free_space_mb,
                       "property": d.property, "group": d.group} for d in storage_result.disks],
        }
    entry["changes"] = record_changes(previous, entry["channels"] if channels_ok else {}, now, flap_hours)
    return entry


def record_changes(previous: dict | None, states: dict[str, str], now: datetime, hours: int) -> dict[str, list[str]]:
    """Online/offline change times per channel within `hours`, carried from
    the previous entry (an unknown state is never a change)."""
    previous = previous or {}
    cutoff = now - timedelta(hours=hours)
    old_states = previous.get("channels") if isinstance(previous.get("channels"), dict) else {}
    old_changes = previous.get("changes") if isinstance(previous.get("changes"), dict) else {}
    changes: dict[str, list[str]] = {}
    for cid in set(old_changes) | set(states):
        kept = [t for t in old_changes.get(cid) or [] if isinstance(t, str) and (_when(t) or cutoff) > cutoff]
        if states.get(cid) in LIVE and old_states.get(cid) in LIVE and states[cid] != old_states[cid]:
            kept.append(now.isoformat())
        if kept:
            changes[cid] = kept[-50:]
    return changes


# ---------- overlay (dashboard) ----------

def _newer(moment: object, report_time: datetime | None) -> bool:
    checked = _when(moment)
    return checked is not None and (report_time is None or checked > report_time)


def overlay(report: dict, entry: dict, now: datetime | None = None) -> dict:
    """A copy of the report with the newer live camera and disk states.

    report["live"] says how current the live data is; data older than the
    report is not used (the scan is newer)."""
    report = copy.deepcopy(report)
    report_time = _when(report.get("check_completed_at") or report.get("check_started_at"))
    channels_newer = _newer(entry.get("channels_at"), report_time) and isinstance(entry.get("channels"), dict)
    storage = entry.get("storage") if isinstance(entry.get("storage"), dict) else None
    storage_newer = (_newer(entry.get("storage_at"), report_time) and storage is not None
                     and storage.get("status") in STORAGE_STATES and isinstance(storage.get("disks"), list))
    report["live"] = {
        "checked_at": str(entry.get("checked_at") or ""),
        "fresh": fresh(entry, now),
        "newer": _newer(entry.get("checked_at"), report_time),
        "reachable": entry.get("reachable") if entry.get("reachable") in {True, False} else None,
        "error": str(entry.get("error") or "")[:40] or None,
        "failures": entry.get("failures") if type(entry.get("failures")) is int else 0,
        "down_since": str(entry.get("down_since") or "")[:40] or None,
        "channels_ok": entry.get("channels_ok") is True,
        "storage_ok": entry.get("storage_ok") is True,
        "channels_used": channels_newer,
        "storage_used": storage_newer,
    }
    overall = dict(report.get("overall") or {})
    critical = list(overall.get("critical_reasons") or [])
    warnings = list(overall.get("warnings") or [])
    unknown = list(overall.get("unknown_checks") or [])
    touched = False
    critical = [c for c in critical if not str(c).startswith(DOWN_REASON)]
    if report["live"]["newer"] and report["live"]["reachable"] is False \
            and report["live"]["failures"] >= DOWN_AFTER_FAILURES:
        # Last scan may say "Healthy"; the NVR has not answered since.
        critical.insert(0, f"{DOWN_REASON} since {_local(entry.get('down_since'))} ({report['live']['error']}).")
        touched = True
    if channels_newer:
        states = {str(k): v for k, v in entry["channels"].items() if v in STATES}
        for row in ((report.get("channel_inventory") or {}).get("channels")) or []:
            if isinstance(row, dict):
                row["reported_status"] = states.get(str(row.get("channel_id")), "NOT_RETURNED")
        offline = sum(v == "OFFLINE" for v in states.values())
        status = dict(report.get("channel_status") or {})
        status.update(status="CHANNEL_STATUS_RETRIEVED", online_count=sum(v == "ONLINE" for v in states.values()),
                      offline_count=offline, unknown_count=sum(v == "UNKNOWN" for v in states.values()))
        report["channel_status"] = status
        warnings = [w for w in warnings if not _OFFLINE_LINE.match(str(w))]
        unknown = [u for u in unknown if not str(u).startswith("Channel status not verified")]
        if offline:
            warnings.insert(0, offline_finding(offline))
        touched = True
        old_flap = flap_warning(report.get("flapping"))
        report["flapping"] = _merge_flapping(report.get("flapping"), entry, report)
        new_flap = flap_warning(report["flapping"])
        if old_flap != new_flap:
            warnings = [w for w in warnings if w != old_flap] + ([new_flap] if new_flap else [])
    if storage_newer:
        per_camera = report.get("per_camera_recording") or {}
        writing = (per_camera.get("enabled") and isinstance(per_camera.get("evidence"), int)
                   and per_camera["evidence"] > 0 and storage.get("status_writing") in STORAGE_STATES)
        status = storage["status_writing"] if writing else storage["status"]
        message = str((storage.get("message_writing") if writing else storage.get("message")) or "")
        merged = dict(report.get("storage") or {})
        merged.update(status=status, message=message, disks=storage["disks"])
        report["storage"] = merged
        critical = [c for c in critical if not str(c).startswith(STORAGE_CRITICAL)]
        warnings = [w for w in warnings if not str(w).startswith(STORAGE_WARNING)]
        unknown = [u for u in unknown if not str(u).startswith(STORAGE_UNKNOWN)]
        add_critical, add_warnings, add_unknown = storage_findings(status, message)
        critical, warnings, unknown = add_critical + critical, warnings + add_warnings, unknown + add_unknown
        touched = True
    if touched and overall:
        overall.update(critical_reasons=critical, warnings=warnings, unknown_checks=unknown,
                       status="CRITICAL" if critical else "WARNING" if warnings else
                       "UNKNOWN" if unknown else "HEALTHY")
        report["overall"] = overall
    return report


def _local(value: object) -> str:
    moment = _when(value)
    return moment.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") if moment else "an unknown time"


def _merge_flapping(flapping: object, entry: dict, report: dict) -> dict:
    """Cameras over the flapping threshold by scans or by live checks (the higher count)."""
    flapping = dict(flapping) if isinstance(flapping, dict) else {}
    needed = flapping.get("changes_needed")
    changes = entry.get("changes") if isinstance(entry.get("changes"), dict) else {}
    if type(needed) is not int or needed < 2 or not changes:
        return flapping
    rows = {str(r.get("channel_id")): r for r in ((report.get("channel_inventory") or {}).get("channels")) or []
            if isinstance(r, dict)}
    cameras = {str(c.get("channel_id")): dict(c) for c in flapping.get("cameras") or [] if isinstance(c, dict)}
    for cid, times in changes.items():
        times = [t for t in times if isinstance(t, str)] if isinstance(times, list) else []
        known = (cameras.get(cid) or {}).get("changes")
        if cid in rows and len(times) >= needed and len(times) > (known if type(known) is int else 0):
            cameras[cid] = {"channel_id": cid, "name": str(rows[cid].get("name") or "")[:80],
                            "changes": len(times), "last_change": times[-1],
                            "reported_status": rows[cid].get("reported_status")}
    for camera in cameras.values():
        if camera["channel_id"] in rows:
            camera["reported_status"] = rows[camera["channel_id"]].get("reported_status")
    flapping["cameras"] = sorted(cameras.values(), key=lambda c: int(c["channel_id"])
                                 if str(c["channel_id"]).isdigit() else 10**6)
    return flapping
