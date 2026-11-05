"""Read-only, password-protected NVR monitoring dashboard."""

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app import events as event_store
from app import notes as notes_store
from app.camera_condition import CONDITIONS, classify, known_condition
from app.duplicates import notes_by_channel
from app.snapshots import STATUSES as PICTURE_STATUSES


REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "reports"))
# Shared with the nvr-scanner service. The dashboard only writes request.json.
SCAN_STATE_DIR = Path(os.environ.get("SCAN_STATE_DIR", "state/scan"))
# Snapshot thumbnails written by the reporter (read-only here).
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "state/snapshots"))
SCANNER_STALE_SECONDS = 45
HOST = "0.0.0.0"
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


HTML = (Path(__file__).with_name("dashboard_ui.html")
        .read_text(encoding="utf-8"))


def _content_security_policy(page: str) -> str:
    """Allow only this page's own inline script and style (by hash)."""
    def hashes(tag: str) -> str:
        found = re.findall(rf"<{tag}>(.*?)</{tag}>", page, flags=re.S)
        return " ".join(
            "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"
            for body in found
        ) or "'none'"
    return ("default-src 'none'; "
            f"script-src {hashes('script')}; style-src {hashes('style')}; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")


CSP = _content_security_policy(HTML)

# <id>_<YYYYmmddTHHMMSS[ffffff]+hhmm>.json, as written by run_nvr_report.
REPORT_NAME = re.compile(
    r"^(?P<id>.+?)_(?P<stamp>\d{8}T\d{6}(?:\d{1,6})?[+-]\d{4})\.json$"
)
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _list_of_text(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _safe_count(value: object) -> int:
    try:
        if isinstance(value, bool):
            return 0
        n = int(value)
        return n if 0 <= n <= 100_000 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _refresh_seconds() -> int:
    try:
        return max(0, min(3600, int(os.environ.get("DASHBOARD_REFRESH_SECONDS", "60"))))
    except ValueError:
        return 60


EVIDENCE_STATUSES = {"RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY"}
UNVERIFIED_STATUSES = {
    "TRACK_NOT_FOUND", "TRACK_MAPPING_UNVERIFIED", "CHECK_INCONCLUSIVE",
    "CHECK_ERROR", "NOT_CHECKED", "NOT_CONFIGURED",
}
HISTORICAL_STATUSES = {
    "HISTORICAL_RECORDING_FOUND", "NO_HISTORICAL_EVIDENCE_IN_WINDOW",
    "HISTORICAL_CHECK_INCONCLUSIVE", "HISTORICAL_CHECK_ERROR",
    "HISTORICAL_NOT_CHECKED", "HISTORICAL_NOT_CONFIGURED",
}


def _public_recording(value: object) -> dict:
    """Normalize newer and pilot per-camera statuses; never leak unknown fields."""
    recording = _as_dict(value)
    legacy = {
        "RECORDING_EVIDENCE_FOUND": "RECORDING",
        "RECORDING_EVIDENCE_FOUND_WITH_TIME_ANOMALY": "RECORDING_EVIDENCE_WITH_ANOMALY",
        "NO_MATCHES_IN_WINDOW": "NO_RECENT_RECORDING_EVIDENCE",
        "SKIPPED_CAMERA_OFFLINE": "NOT_CHECKED",
        "NOT_CHECKED_TRACK_ID_NOT_CONFIGURED": "NOT_CONFIGURED",
    }

    raw = str(recording.get("status") or "NOT_CHECKED")
    status = legacy.get(raw, raw)
    if status not in (EVIDENCE_STATUSES | UNVERIFIED_STATUSES | {"NO_RECENT_RECORDING_EVIDENCE"}):
        status = "CHECK_INCONCLUSIVE"
    window = _as_dict(recording.get("requested_window_utc"))
    return {
        "status": status,
        "message": str(recording.get("message") or ("No per-camera recording result in this report." if not recording else ""))[:500],
        "track_id": recording.get("track_id"),
        "track_verified": bool(recording.get("track_verified")) if "track_verified" in recording else None,
        "num_matches": recording.get("num_matches"),
        "total_matches": recording.get("total_matches"),
        "most_recent_segment_end": recording.get("most_recent_segment_end") or recording.get("latest_segment_end"),
        "most_recent_segment_start": recording.get("most_recent_segment_start") or recording.get("latest_segment_start"),
        "checked_at": recording.get("checked_at") or recording.get("check_time"),
        "window_minutes": recording.get("window_minutes"),
        "more_available": recording.get("more_available") is True,
        "latest_segment_complete": (recording.get("latest_segment_complete")
                                    if type(recording.get("latest_segment_complete")) is bool
                                    else None),
        "requested_window_utc": {
            "start": window.get("start"), "end": window.get("end")
        },
        "anomalies": _list_of_text(recording.get("anomalies"))[:10],
    }


def _public_historical(value: object) -> dict:
    """Expose a small, trusted historical schema; legacy reports stay unverified."""
    raw = _as_dict(value)
    status = raw.get("status", "HISTORICAL_NOT_CHECKED")
    if not isinstance(status, str) or status not in HISTORICAL_STATUSES:
        status = "HISTORICAL_CHECK_INCONCLUSIVE"
    window = _as_dict(raw.get("requested_window_utc"))
    intervals = raw.get("intervals_examined")
    if not isinstance(intervals, list):
        intervals = []
    return {
        "status": status,
        "message": str(raw.get("message") or "Historical check not included in this report.")[:500],
        "track_id": raw.get("track_id"),
        "checked_at": raw.get("checked_at"),
        "num_matches": raw.get("num_matches"),
        "most_recent_segment_end": raw.get("most_recent_segment_end"),
        "most_recent_segment_start": raw.get("most_recent_segment_start"),
        "search_complete": raw.get("search_complete") is True,
        "from_cache": raw.get("from_cache") is True,
        "latest_segment_complete": (raw.get("latest_segment_complete")
                                    if type(raw.get("latest_segment_complete")) is bool
                                    else None),
        "requested_window_utc": {"start": window.get("start"), "end": window.get("end")},
        "intervals_examined": [{
            "start": _as_dict(i).get("start"), "end": _as_dict(i).get("end"),
            "status": str(_as_dict(i).get("status", ""))[:80],
        } for i in intervals[:8]],
    }

RETENTION_STATUSES = {
    "RETENTION_VERIFIED", "RETENTION_PARTIAL", "RETENTION_PENDING", "RETENTION_STALE",
    "RETENTION_UNSUPPORTED", "RETENTION_INCONCLUSIVE", "RETENTION_ERROR",
    "RETENTION_UNVERIFIED", "RETENTION_NOT_CONFIGURED",
}


def _public_retention(value: object) -> dict:
    """Expose bounded, explicit recording dates; never invent unknown days."""
    raw = _as_dict(value)
    status = raw.get("status", "RETENTION_NOT_CONFIGURED")
    if not isinstance(status, str) or status not in RETENTION_STATUSES:
        status = "RETENTION_INCONCLUSIVE"

    def day(value):
        if not isinstance(value, str) or len(value) != 10:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None

    def number(value):
        return value if type(value) is int and 0 <= value <= 90 else None

    rows = []
    if isinstance(raw.get("calendar"), list):
        for item in raw["calendar"][:90]:
            item = _as_dict(item)
            date_text = day(item.get("date"))
            if date_text:
                present = item.get("has_recording")
                rows.append({"date": date_text,
                             "has_recording": present if type(present) is bool else None})
    today = _as_dict(raw.get("today"))
    today_row = None
    if day(today.get("date")):
        present = today.get("has_recording")
        today_row = {"date": day(today.get("date")),
                     "has_recording": present if type(present) is bool else None}
    return {
        "status": status, "message": str(raw.get("message") or "Calendar not yet collected.")[:250],
        "today": today_row,
        "checked_at": str(raw.get("checked_at") or "")[:40],
        "as_of_date": day(raw.get("as_of_date")),
        "window_days": number(raw.get("window_days")),
        "days_with_footage": number(raw.get("days_with_footage")),
        "days_examined": number(raw.get("days_examined")),
        "oldest_verified_date": day(raw.get("oldest_verified_date")),
        "oldest_verified_age_days": number(raw.get("oldest_verified_age_days")),
        "from_cache": raw.get("from_cache") is True,
        "calendar": rows,
    }


def _public_camera_summary(value: object) -> dict:
    """Whitelist summary metadata; keep credentials and arbitrary JSON out of the API."""
    raw = _as_dict(value)
    return {
        "enabled": raw.get("enabled") is True,
        "window_minutes": _safe_count(raw.get("window_minutes")),
        "track_mapping_status": str(raw.get("track_mapping_status") or "UNVERIFIED")[:80],
        "nvr_clock_offset_seconds_approx": raw.get("nvr_clock_offset_seconds_approx")
        if type(raw.get("nvr_clock_offset_seconds_approx")) is int
        and -86400 <= raw["nvr_clock_offset_seconds_approx"] <= 86400 else None,
    }


def _number(value: object, low: float = 0, high: float = 10**9) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and low <= value <= high else None


def _public_snapshot(value: object) -> dict:
    raw = _as_dict(value)
    status = raw.get("status") if raw.get("status") in PICTURE_STATUSES else "PICTURE_NOT_CONFIGURED"
    return {"status": status, "message": str(raw.get("message") or "")[:200],
            "checked_at": str(raw.get("checked_at") or "")[:40],
            "brightness": _number(raw.get("brightness"), 0, 255),
            "contrast": _number(raw.get("contrast"), 0, 255),
            "mode": raw.get("mode") if raw.get("mode") in {"day", "night"} else None,
            "has_image": raw.get("has_image") is True}


SMART_STATUSES = {"HEALTHY", "WARNING", "CRITICAL", "UNVERIFIED", "NOT_CONFIGURED"}


def _public_smart(value: object) -> dict:
    raw = _as_dict(value)
    disks = []
    for disk in raw.get("disks") or []:
        disk = _as_dict(disk)
        disks.append({"id": str(disk.get("id", ""))[:20],
                      "status": disk.get("status") if disk.get("status") in SMART_STATUSES else "UNVERIFIED",
                      "message": str(disk.get("message") or "")[:200],
                      "temperature_c": _number(disk.get("temperature_c"), 0, 150),
                      "power_on_days": _number(disk.get("power_on_days"), 0, 100000),
                      "reallocated": _number(disk.get("reallocated")),
                      "pending": _number(disk.get("pending")),
                      "uncorrectable": _number(disk.get("uncorrectable"))})
    return {"status": raw.get("status") if raw.get("status") in SMART_STATUSES else "NOT_CONFIGURED",
            "message": str(raw.get("message") or "")[:400], "checked_at": str(raw.get("checked_at") or "")[:40],
            "from_cache": raw.get("from_cache") is True, "disks": disks[:64]}


def _public_changes(value: object) -> dict:
    raw = _as_dict(value)
    changes = []
    for change in raw.get("changes") or []:
        change = _as_dict(change)
        if change.get("type") in {"added", "removed", "renamed", "ip_changed"}:
            changes.append({"type": change["type"], "channel_id": str(change.get("channel_id", ""))[:8],
                            "old": str(change.get("old") or "")[:80], "new": str(change.get("new") or "")[:80]})
    return {"status": str(raw.get("status") or "NOT_COMPARED")[:20],
            "compared_with": str(raw.get("compared_with") or "")[:40], "changes": changes[:100]}


def _public_report(report: dict, path: Path, notes: dict | None = None) -> dict:
    """Expose only dashboard fields; never expose arbitrary report contents."""
    nvr = _as_dict(report.get("nvr"))
    device = _as_dict(report.get("device_info"))
    channel_inventory = _as_dict(report.get("channel_inventory"))
    channel_status = _as_dict(report.get("channel_status"))
    storage = _as_dict(report.get("storage"))
    recording = _as_dict(report.get("recording"))
    overall = _as_dict(report.get("overall"))

    channels = []
    raw_channels = channel_inventory.get("channels", [])
    if isinstance(raw_channels, list):
        for channel in raw_channels:
            channel = _as_dict(channel)
            # Never elevate old reports to successful recording checks.
            per_camera = (channel.get("recording_result")
                          or channel.get("camera_recording")  # Existing Ubuntu pilot schema.
                          or channel.get("recording_check")
                          or channel.get("recording")
                          or ({"status": channel["recording_status"]}
                              if "recording_status" in channel else None))
            channels.append({
                "channel_id": str(channel.get("channel_id", "")),
                "name": str(channel.get("name") or ""),
                "camera_ip": str(channel.get("camera_ip") or ""),
                "management_port": channel.get("management_port"),
                "recording_result": _public_recording(per_camera),
                "historical_recording_result": _public_historical(
                    channel.get("historical_recording_result")
                ),
                "retention_result": _public_retention(channel.get("retention_result")),
                "recording_expectation": (channel.get("recording_expectation")
                                          if isinstance(channel.get("recording_expectation"), str)
                                          and channel.get("recording_expectation") in {"continuous", "event", "unspecified"}
                                          else "unspecified"),
                "reported_status": str(
                    channel.get("reported_status", "UNKNOWN")
                ),
                "snapshot_result": _public_snapshot(channel.get("snapshot_result")),
            })
            channels[-1]["condition"] = classify(channels[-1])
    # "Same camera as ch51" / "Same name as ch22" (app.duplicates; no IPs).
    duplicate_notes = notes_by_channel(report.get("duplicates"))
    for channel in channels:
        channel["duplicate_notes"] = [str(note)[:80] for note in duplicate_notes.get(channel["channel_id"], [])][:4]
    nvr_id = str(nvr.get("id", path.name.split("_")[0]))
    if notes is not None:
        notes_store.overlay(nvr_id, channels, notes, known_condition())
    smart = _public_smart(storage.get("smart"))
    smart_by_disk = {d["id"]: d for d in smart["disks"]}

    disks = []
    raw_disks = storage.get("disks", [])
    if isinstance(raw_disks, list):
        for disk in raw_disks:
            disk = _as_dict(disk)
            disks.append({
                "id": str(disk.get("id", "")),
                "name": str(disk.get("name", "")),
                "type": str(disk.get("type", "")),
                "reported_status": str(
                    disk.get("reported_status", "UNKNOWN")
                ),
                "capacity_mb": disk.get("capacity_mb"),
                "free_space_mb": disk.get("free_space_mb"),
                "property": str(disk.get("property", "")),
                "group": str(disk.get("group", "")),
                "smart": smart_by_disk.get(str(disk.get("id", "")).split(" (", 1)[0]),
            })
    device_status = _as_dict(report.get("device_status"))
    restart = _as_dict(report.get("restart"))

    return {
        "id": str(nvr.get("id", path.name.split("_")[0])),
        "name": str(nvr.get("name") or ""),
        "ip": str(nvr.get("ip", "")),
        "http_port": nvr.get("http_port"),
        "server_port": nvr.get("server_port"),
        "model": str(device.get("model") or ""),
        "firmware_version": str(device.get("firmware_version") or ""),
        "channel_source": str(channel_inventory.get("channel_source") or "CHANNEL_INVENTORY"),
        "overall_status": str(
            _as_dict(overall).get("status", "UNKNOWN")
        ),
        "critical_reasons": _list_of_text(overall.get("critical_reasons")),
        "warnings": _list_of_text(overall.get("warnings")),
        "unknown_checks": _list_of_text(overall.get("unknown_checks")),
        "expected_channels": channel_inventory.get("expected_count"),
        "configured_channels": channel_inventory.get("configured_count"),
        "online_channels": _safe_count(channel_status.get("online_count")),
        "offline_channels": _safe_count(channel_status.get("offline_count")),
        "unknown_channels": _safe_count(channel_status.get("unknown_count")),
        "storage_status": str(storage.get("status", "UNKNOWN")),
        "storage_message": str(storage.get("message", "")),
        "recording_status": str(recording.get("status", "UNKNOWN")),
        "recording_message": str(recording.get("message", "")),
        "recording_track_id": recording.get("track_id"),
        "per_camera_recording": _public_camera_summary(
            report.get("per_camera_recording")
            or report.get("camera_recording_summary")
            or report.get("recording_summary")
        ),
        "last_check": str(
            report.get("check_completed_at")
            or report.get("check_started_at")
            or ""
        ),
        "report_file": path.name,
        "channels": channels,
        "condition_counts": {code: sum(ch["condition"]["code"] == code for ch in channels)
                             for code in CONDITIONS},
        "disks": disks,
        "smart": smart,
        "uptime_seconds": _number(device_status.get("uptime_seconds"), 0, 10**10),
        "cpu_percent": _number(device_status.get("cpu_percent"), 0, 100),
        "restart": {"detected": restart.get("detected") is True,
                    "boot_time": str(restart.get("boot_time") or "")[:40]},
        "inventory_changes": _public_changes(report.get("inventory_changes")),
        "snapshots_enabled": _as_dict(report.get("snapshots")).get("enabled") is True,
        "nvr_notes": [notes_store.public_note(n) for n in reversed(notes.get("log", []))
                      if n["nvr"] == nvr_id and n.get("channel") is None][:5] if notes is not None else [],
        "nvr_known_issue": notes_store.public_issue(notes_store.issue_for(
            notes_store.active_issues(notes), nvr_id, None)) if notes is not None else None,
    }


LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")
STALE_FALLBACK = timedelta(hours=26)     # No automatic scan schedule known.
SCAN_RUN_ALLOWANCE = timedelta(hours=2)  # Time a fleet scan may take to reach an NVR.
STALE_TOLERANCE = timedelta(minutes=10)


def _stale_cutoff(now: datetime, scan_times: list[str]) -> tuple[datetime, datetime | None]:
    """Oldest report time still counted as current, and the scan slot behind it.

    With automatic scans (NVR_SCAN_TIMES, e.g. every 2 hours), a report is
    stale once a later scheduled scan should have replaced it, allowing
    SCAN_RUN_ALLOWANCE for a fleet scan to reach the NVR. A fixed 26 hours
    would hide a stopped scanner for a whole day. Without a known schedule
    the old 26-hour rule applies.
    """
    times = []
    for text in scan_times:
        if isinstance(text, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
            times.append((int(text[:2]), int(text[3:])))
    if not times:
        return now - STALE_FALLBACK, None
    reference = (now - SCAN_RUN_ALLOWANCE).astimezone(LOCAL_TZ)
    slots = [datetime(day.year, day.month, day.day, h, m, tzinfo=LOCAL_TZ)
             for day in (reference.date(), reference.date() - timedelta(days=1))
             for h, m in times]
    slot = max(s for s in slots if s <= reference)  # Yesterday's slots always qualify.
    return slot - STALE_TOLERANCE, slot


EVENT_SERVICE_STALE = timedelta(minutes=2)


def _attach_events(nvrs: list[dict]) -> list[dict]:
    """Recent real-time events and stream state per NVR (nvr-events service)."""
    status = event_store.read_status()
    streams = status.get("nvrs") if isinstance(status.get("nvrs"), dict) else {}
    try:
        updated = datetime.fromisoformat(status["updated_at"])
        service_alive = datetime.now(timezone.utc) - updated < EVENT_SERVICE_STALE
    except (KeyError, TypeError, ValueError):
        service_alive = False
    recent = event_store.recent_events(hours=24)
    for nvr in nvrs:
        state = streams.get(nvr["id"])
        nvr["event_stream"] = None if not isinstance(state, dict) else {
            "connected": service_alive and state.get("connected") is True,
            "since": str(state.get("since") or "")[:40],
            "last_seen": str(state.get("last_seen") or "")[:40],
            "last_error": "SERVICE_STOPPED" if not service_alive else str(state.get("last_error") or "")[:40],
        }
        nvr["events"] = [e for e in recent if e["nvr"] == nvr["id"]][:20]
    return recent


def _is_fresh(timestamp: str, cutoff: datetime | None = None) -> bool:
    try:
        checked = datetime.fromisoformat(timestamp)
        now = datetime.now(timezone.utc)
        age = now - checked.astimezone(timezone.utc)
        if cutoff is None:
            cutoff = now - STALE_FALLBACK
        return checked.tzinfo is not None and age.total_seconds() >= 0 and checked >= cutoff
    except (ValueError, TypeError):
        return False


def _read_fleet_run_state() -> tuple[dict[str, str], datetime | None, int]:
    """Read fixed-code run failures; never expose raw manifest content."""
    path = REPORTS_DIR / "fleet_latest_run.json"
    if not path.exists():
        return {}, None, 0  # Compatible with saved reports from older releases.
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("Unexpected fleet-run manifest")
        started = datetime.fromisoformat(manifest["started_at"])
        completed = datetime.fromisoformat(manifest["completed_at"])
        if (started.tzinfo is None or completed.tzinfo is None
                or started > completed
                or completed.astimezone(timezone.utc) >
                datetime.now(timezone.utc).replace(microsecond=999999)):
            raise ValueError("Invalid fleet-run timestamps")
        devices = manifest.get("devices")
        if not isinstance(devices, list) or len(devices) > 1000:
            raise ValueError("Invalid device results")
        codes = {"PROCESS_TIMEOUT", "PROCESS_START_FAILURE", "NO_SAVED_REPORT",
                 "REPORT_READ_ERROR", "REPORT_UNAVAILABLE", "LOCK_CONFLICT"}
        failed = {}
        for item in devices:
            if not isinstance(item, dict):
                raise ValueError("Invalid device row")
            device_id = item.get("id")
            # Same rule as the inventory loader (app.config).
            if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
                raise ValueError("Invalid device ID")
            if item.get("result") == "ERROR":
                reason = item.get("reason")
                failed[device_id] = reason if reason in codes else "REPORT_UNAVAILABLE"
            elif item.get("result") != "OK":
                raise ValueError("Invalid run status")
        return failed, started.astimezone(timezone.utc), 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {}, None, 1


def _checked_after(timestamp: str, started_at: datetime) -> bool:
    try:
        checked = datetime.fromisoformat(timestamp)
        return checked.tzinfo is not None and checked.astimezone(timezone.utc) >= started_at
    except (ValueError, TypeError):
        return False


def _name_timestamp(stamp: str) -> datetime | None:
    for pattern in ("%Y%m%dT%H%M%S%f%z", "%Y%m%dT%H%M%S%z"):
        try:
            return datetime.strptime(stamp, pattern)
        except ValueError:
            continue
    return None


def _report_candidates() -> tuple[list[list[Path]], list[Path]]:
    """Group timestamp-named reports per NVR, newest first.

    Reports accumulate every day and are never deleted, so the dashboard must
    not parse the whole history on every refresh. Files without the standard
    timestamped name (rare, manual copies) are returned separately.
    """
    grouped: dict[str, list[tuple[datetime, str, Path]]] = {}
    other: list[Path] = []
    if not REPORTS_DIR.is_dir():
        return [], other
    # Any inventory ID is valid (app.config), not only "nvr-…". Other JSON in
    # the folder (fleet_latest_run.json, track-verification-*.json) is not a
    # device report and must not be counted as an unreadable one.
    for path in REPORTS_DIR.glob("*_*.json"):
        match = REPORT_NAME.match(path.name)
        stamp = _name_timestamp(match["stamp"]) if match else None
        if stamp is None or not DEVICE_ID.fullmatch(match["id"]):
            if path.name.startswith("nvr-"):
                other.append(path)  # Manual copies, as before.
            continue
        grouped.setdefault(match["id"], []).append((stamp, path.name, path))
    ordered = [
        [path for _, _, path in sorted(items, key=lambda item: (item[0], item[1]), reverse=True)]
        for items in grouped.values()
    ]
    return ordered, other


def _load_report(path: Path) -> tuple[str, int, dict] | None:
    """Return (nvr_id, ordering_ns, report), or None if unusable."""
    try:
        with path.open("r", encoding="utf-8") as file:
            report = json.load(file)
        if not isinstance(report, dict):
            return None
        nvr_id = str(_as_dict(report.get("nvr")).get("id", ""))
        if not nvr_id:
            return None
        recorded = report.get("check_completed_at") or report.get("check_started_at")
        try:
            completed = datetime.fromisoformat(recorded)
            if completed.tzinfo is None:
                raise ValueError("Timezone missing")
            ordering_ns = int(completed.timestamp() * 1_000_000_000)
        except (TypeError, ValueError, OverflowError):
            ordering_ns = path.stat().st_mtime_ns
        return nvr_id, ordering_ns, report
    except (OSError, ValueError, RecursionError):
        return None


def _latest_check(values: list[str]) -> str | None:
    """Newest ISO timestamp by actual time, not by string order."""
    best = None
    for value in values:
        try:
            moment = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            continue
        if best is None or moment > best[0]:
            best = (moment, value)
    return best[1] if best else None


def read_dashboard_data() -> dict:
    """Read saved reports and annotate newer failed fleet attempts."""
    latest: dict[str, tuple[int, dict, Path]] = {}
    report_errors = 0

    def consider(path: Path) -> bool:
        nonlocal report_errors
        loaded = _load_report(path)
        if loaded is None:
            report_errors += 1
            return False
        nvr_id, ordering_ns, report = loaded
        previous = latest.get(nvr_id)
        if previous is None or ordering_ns > previous[0]:
            latest[nvr_id] = (ordering_ns, report, path)
        return True

    grouped, other = _report_candidates()
    for paths in grouped:
        # Newest name first; fall back to an older file only if it is unreadable.
        for path in paths:
            if consider(path):
                break
    for path in other:
        consider(path)

    notes = notes_store.load()
    nvrs = [
        _public_report(report, path, notes)
        for _, report, path in latest.values()
    ]
    failed, run_started, manifest_errors = _read_fleet_run_state()
    report_errors += manifest_errors
    scan = read_scan_status()
    stale_cutoff, stale_slot = _stale_cutoff(datetime.now(timezone.utc), scan["scan_times"])
    for nvr in nvrs:
        nvr["last_run_failed"] = False
        nvr["last_run_failure_code"] = None
        nvr["last_known_overall_status"] = nvr["overall_status"]
        if (run_started is not None and nvr["id"] in failed
                and not _checked_after(nvr["last_check"], run_started)):
            nvr["last_run_failed"] = True
            nvr["last_run_failure_code"] = failed[nvr["id"]]
            nvr["overall_status"] = "ERROR"
    known_ids = {nvr["id"] for nvr in nvrs}
    if run_started is not None:
        for device_id, failure_code in failed.items():
            if device_id in known_ids:
                continue
            placeholder = _public_report({"nvr": {"id": device_id},
                                          "overall": {"status": "ERROR"}},
                                         Path("no_saved_report.json"))
            placeholder.update(last_run_failed=True,
                               last_run_failure_code=failure_code,
                               last_known_overall_status=None)
            nvrs.append(placeholder)
    nvrs.sort(key=lambda item: item["id"])
    for nvr in nvrs:
        nvr["stale"] = bool(nvr["last_run_failed"] or not nvr["last_check"]
                            or not _is_fresh(nvr["last_check"], stale_cutoff))
    events = _attach_events(nvrs)

    # Preserve last-known camera data in the device card, but never count it
    # as a fresh observation after a failed fleet run.
    current_nvrs = [nvr for nvr in nvrs if not nvr["last_run_failed"]]
    all_channels = [ch for nvr in current_nvrs for ch in nvr["channels"]]
    rec_statuses = [ch["recording_result"]["status"] for ch in all_channels]
    historical_statuses = [ch["historical_recording_result"]["status"] for ch in all_channels]
    retention_statuses = [ch["retention_result"]["status"] for ch in all_channels]
    summary = {
        "total_nvrs": len(nvrs),
        "healthy": sum(nvr["overall_status"] == "HEALTHY" for nvr in current_nvrs),
        "warning": sum(nvr["overall_status"] == "WARNING" for nvr in current_nvrs),
        "critical": sum(nvr["overall_status"] == "CRITICAL" for nvr in current_nvrs),
        "latest_run_failed": sum(nvr["last_run_failed"] for nvr in nvrs),
        "latest_run_reporting": len(current_nvrs),
        "last_known_cameras_on_failed_nvrs": sum(len(nvr["channels"]) for nvr in nvrs
                                               if nvr["last_run_failed"]),
        "condition_counts": {code: sum(nvr["condition_counts"][code] for nvr in current_nvrs)
                             for code in CONDITIONS},
        "cameras_online": sum(nvr["online_channels"] or 0 for nvr in current_nvrs),
        "cameras_offline": sum(nvr["offline_channels"] or 0 for nvr in current_nvrs),
        "cameras_unknown": sum(nvr["unknown_channels"] or 0 for nvr in current_nvrs),
        "recording_evidence": sum(s in EVIDENCE_STATUSES for s in rec_statuses),
        "recording_no_evidence": rec_statuses.count("NO_RECENT_RECORDING_EVIDENCE"),
        "recording_unverified": sum(s in UNVERIFIED_STATUSES for s in rec_statuses),
        "recording_errors": rec_statuses.count("CHECK_ERROR"),
        "recording_not_checked": sum(s in {"NOT_CHECKED", "NOT_CONFIGURED"} for s in rec_statuses),
        # Expected cameras the latest report could not list at all (NVR
        # unreachable). They have no recording result and must stay visible.
        "cameras_without_result": sum(
            max(nvr["expected_channels"] - len(nvr["channels"]), 0)
            for nvr in current_nvrs if isinstance(nvr["expected_channels"], int)
        ),
        "online_missing_expected": sum(
            ch["reported_status"] == "ONLINE" and ch["recording_expectation"] == "continuous"
            and ch["recording_result"]["status"] == "NO_RECENT_RECORDING_EVIDENCE"
            for ch in all_channels
        ),
        "retention_verified": retention_statuses.count("RETENTION_VERIFIED"),
        "retention_partial": retention_statuses.count("RETENTION_PARTIAL"),
        "retention_pending": retention_statuses.count("RETENTION_PENDING") + retention_statuses.count("RETENTION_STALE"),
        "retention_unsupported": retention_statuses.count("RETENTION_UNSUPPORTED"),
        "retention_errors": retention_statuses.count("RETENTION_ERROR") + retention_statuses.count("RETENTION_INCONCLUSIVE"),
        "retention_not_configured": retention_statuses.count("RETENTION_NOT_CONFIGURED"),
        "historical_evidence": historical_statuses.count("HISTORICAL_RECORDING_FOUND"),
        "historical_no_evidence": historical_statuses.count("NO_HISTORICAL_EVIDENCE_IN_WINDOW"),
        "historical_unverified": sum(s not in {"HISTORICAL_RECORDING_FOUND", "NO_HISTORICAL_EVIDENCE_IN_WINDOW"} for s in historical_statuses),
        "historical_errors": historical_statuses.count("HISTORICAL_CHECK_ERROR"),
        # A deferred historical search is not a failed search. Expose the
        # operational backlog separately from the legacy aggregate field.
        "historical_pending_no_recent": sum(
            ch["recording_result"]["status"] == "NO_RECENT_RECORDING_EVIDENCE"
            and ch["historical_recording_result"]["status"] in {
                "HISTORICAL_NOT_CHECKED", "HISTORICAL_NOT_CONFIGURED",
            }
            for ch in all_channels
        ),
        "historical_inconclusive": historical_statuses.count("HISTORICAL_CHECK_INCONCLUSIVE"),
        "historical_skipped_recent": sum(
            ch["recording_result"]["status"] in EVIDENCE_STATUSES
            and ch["historical_recording_result"]["status"] == "HISTORICAL_NOT_CHECKED"
            for ch in all_channels
        ),
        "stale_reports": sum(nvr["stale"] for nvr in nvrs),
        # Reports older than this are stale; stale_slot is the scheduled scan
        # that should have replaced them (None: 26-hour rule, no schedule).
        "stale_cutoff": stale_cutoff.isoformat(),
        "stale_slot": stale_slot.isoformat() if stale_slot else None,
        "last_successful_report": _latest_check([
            nvr["last_check"] for nvr in current_nvrs
            if nvr["overall_status"] in {"HEALTHY", "WARNING", "CRITICAL"}
        ]),
        "window_minutes": max(
            (nvr["per_camera_recording"]["window_minutes"] for nvr in current_nvrs
             if nvr["per_camera_recording"]["enabled"]),
            default=None,
        ),
        # Offline cameras with stored footage, recent or older (filter "Offline, footage stored").
        "offline_with_evidence": sum(ch["reported_status"] == "OFFLINE"
                                     and (ch["recording_result"]["status"] in EVIDENCE_STATUSES
                                          or ch["historical_recording_result"]["status"] == "HISTORICAL_RECORDING_FOUND")
                                     for ch in all_channels),
        # Offline but recorded in the search window: the "Offline, still recording" condition.
        "offline_recording_now": sum(ch["reported_status"] == "OFFLINE"
                                     and ch["recording_result"]["status"] in EVIDENCE_STATUSES
                                     for ch in all_channels),
        "storage_warnings": sum(nvr["storage_status"] == "WARNING" for nvr in current_nvrs),
        "smart_problems": sum(nvr["smart"]["status"] in {"WARNING", "CRITICAL"} for nvr in current_nvrs),
        "events_24h": len(events),
        "event_streams_down": sum(bool(nvr["event_stream"]) and not nvr["event_stream"]["connected"]
                                  for nvr in nvrs),
        "known_issues": len(notes_store.active_issues(notes)),
        # Known-issue cameras without recent footage: handled, but still not
        # recording, so the headline must never claim "every camera records".
        "known_not_recording": sum(ch["condition"]["code"] == "KNOWN_ISSUE"
                                   and ch["recording_result"]["status"] not in EVIDENCE_STATUSES
                                   for ch in all_channels),
        "camera_changes": sum(len(nvr["inventory_changes"]["changes"]) for nvr in current_nvrs),
        "restarts": sum(nvr["restart"]["detected"] for nvr in current_nvrs),
        "storage_critical": sum(nvr["storage_status"] == "CRITICAL" for nvr in current_nvrs),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "refresh_seconds": _refresh_seconds(),
        "summary": summary,
        "nvrs": nvrs,
        "report_errors": report_errors,
        "scan": scan,
    }


# ---------- on-demand scans (requests only; the nvr-scanner service runs them) ----------

SCAN_RESULTS = {"OK", "ERRORS", "FAILED", "TIMEOUT", "BUSY", "INTERRUPTED"}
SCAN_TRIGGERS = {"manual", "schedule"}
EMAIL_RESULTS = {"SENT", "SKIPPED", "FAILED", "NOT SENT"}
HEALTH = {"HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"}
_scan_lock = threading.Lock()


def _when(value: object) -> str | None:
    try:
        moment = datetime.fromisoformat(value)
        return moment.astimezone(timezone.utc).isoformat() if moment.tzinfo else None
    except (TypeError, ValueError):
        return None


def _target(value: object) -> str | None:
    return value if isinstance(value, str) and (value == "all" or DEVICE_ID.fullmatch(value)) else None


def _one_of(value: object, allowed: set[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def read_scan_status() -> dict:
    """Whitelisted view of the scanner's status file; never raw content."""
    raw = {}
    try:
        loaded = json.loads((SCAN_STATE_DIR / "status.json").read_text(encoding="utf-8"))
        raw = loaded if isinstance(loaded, dict) and loaded.get("schema_version") == 1 else {}
    except (OSError, ValueError):
        pass
    now = datetime.now(timezone.utc)
    heartbeat = _when(raw.get("heartbeat_at"))
    available = bool(heartbeat) and 0 <= (now - datetime.fromisoformat(heartbeat)).total_seconds() < SCANNER_STALE_SECONDS

    def times(value: object) -> list[str]:
        return [t for t in value if isinstance(t, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t)][:48] \
            if isinstance(value, list) else []

    current = None
    if isinstance(raw.get("current"), dict) and _target(raw["current"].get("target")):
        c = raw["current"]
        progress = _as_dict(c.get("progress"))
        current = {
            "target": c["target"], "trigger": _one_of(c.get("trigger"), SCAN_TRIGGERS),
            "email": c.get("email") is True, "started_at": _when(c.get("started_at")),
            "progress": {"done": _safe_count(progress.get("done")), "total": _safe_count(progress.get("total")),
                         "nvr": _target(progress.get("nvr"))},
        }
    last = None
    if isinstance(raw.get("last"), dict) and _target(raw["last"].get("target")):
        l = raw["last"]
        last = {
            "target": l["target"], "trigger": _one_of(l.get("trigger"), SCAN_TRIGGERS),
            "email": l.get("email") is True, "result": _one_of(l.get("result"), SCAN_RESULTS),
            "email_result": _one_of(l.get("email_result"), EMAIL_RESULTS),
            "overall": _one_of(l.get("overall"), HEALTH),
            "execution_errors": _safe_count(l.get("execution_errors")) if l.get("execution_errors") is not None else None,
            "started_at": _when(l.get("started_at")), "finished_at": _when(l.get("finished_at")),
        }
    last_email = None
    if isinstance(raw.get("last_email"), dict):
        last_email = {"result": _one_of(raw["last_email"].get("result"), EMAIL_RESULTS),
                      "finished_at": _when(raw["last_email"].get("finished_at"))}
    rejected = None
    if isinstance(raw.get("rejected"), dict):
        at = _when(raw["rejected"].get("at"))
        if at and now - datetime.fromisoformat(at) < timedelta(minutes=10):
            rejected = {"reason": str(raw["rejected"].get("reason") or "")[:160], "at": at}
    queued = None
    request = None
    try:
        request = json.loads((SCAN_STATE_DIR / "request.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if isinstance(request, dict) and _target(request.get("target")):
        queued = {"target": request["target"], "requested_at": _when(request.get("requested_at"))}
    cooldown_until = _when(raw.get("cooldown_until"))
    cooldown = max(0, int((datetime.fromisoformat(cooldown_until) - now).total_seconds())) if cooldown_until else 0
    return {
        "available": available,
        "on_demand_enabled": raw.get("on_demand_enabled") is True,
        "scan_times": times(raw.get("scan_times")),
        "email_times": times(raw.get("email_times")),
        "next_scheduled_at": _when(raw.get("next_scheduled_at")),
        "current": current, "queued": queued, "last": last, "last_email": last_email,
        "rejected": rejected, "cooldown_seconds": min(cooldown, 3600),
    }


def request_scan(target: object) -> tuple[int, dict]:
    """Queue one scan for the scanner service. Returns (HTTP status, body)."""
    target = _target(target)
    if target is None:
        return 400, {"error": "Choose all NVRs or one NVR ID."}
    with _scan_lock:
        status = read_scan_status()
        if not status["available"]:
            return 503, {"error": "The scanner service is not running.", "scan": status}
        if not status["on_demand_enabled"]:
            return 403, {"error": "On-demand scans are turned off (NVR_ON_DEMAND_SCANS).", "scan": status}
        if status["current"] or status["queued"]:
            return 409, {"error": "A scan is already running or waiting to start.", "scan": status}
        if status["cooldown_seconds"]:
            return 429, {"error": f"Wait {status['cooldown_seconds']} s before the next scan.", "scan": status}
        request = {"schema_version": 1, "id": uuid.uuid4().hex, "target": target,
                   "requested_at": datetime.now(timezone.utc).isoformat()}
        SCAN_STATE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=SCAN_STATE_DIR,
                                         prefix=".request", suffix=".tmp", delete=False) as handle:
            json.dump(request, handle)
            temporary = Path(handle.name)
        os.replace(temporary, SCAN_STATE_DIR / "request.json")
        return 202, {"queued": target, "scan": read_scan_status()}


def save_note(body: dict, by: str) -> tuple[int, dict]:
    """Known issue / fixed / repair note from the dashboard. Returns (HTTP status, body)."""
    action = body.get("action")
    nvr_id = body.get("nvr")
    channel = body.get("channel")
    channel = str(channel) if isinstance(channel, (str, int)) and str(channel) not in {"", "all"} else None
    if action not in {"known", "fixed", "note"} or not notes_store.valid_target(nvr_id, channel):
        return 400, {"error": "Choose an NVR, a camera and an action."}
    if action == "known":
        until = notes_store.parse_until(str(body.get("until") or ""))
        if until == "invalid":
            return 400, {"error": "Until must be like 7d, 2w or YYYY-MM-DD (at most 180 days)."}
        item = notes_store.add_known_issue(nvr_id, channel, str(body.get("text") or ""), until, by)
        return 200, {"ok": True, "known_issue": notes_store.public_issue(item)}
    if action == "fixed":
        removed = notes_store.clear_known_issue(nvr_id, channel, by, str(body.get("text") or ""))
        return 200, {"ok": True, "cleared": removed}
    if not notes_store.clean_text(body.get("text")):
        return 400, {"error": "Write a note first."}
    notes_store.add_note(nvr_id, channel, str(body.get("text")), by)
    return 200, {"ok": True}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "NVRDashboard"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        # Do not log request headers or credentials.
        super().log_message(fmt, *args)

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self) -> None:
        body = b"Authentication required.\n"
        self.send_response(401)
        self.send_header(
            "WWW-Authenticate",
            'Basic realm="NVR Tracking Dashboard", charset="UTF-8"',
        )
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, separator, token = header.partition(" ")
        if not separator or scheme.lower() != "basic":
            return False

        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False

        username, separator, password = decoded.partition(":")
        if not separator:
            return False

        # Compare bytes: compare_digest() raises TypeError for str values with
        # non-ASCII characters (e.g. typed with a Khmer keyboard layout), which
        # dropped the connection instead of answering 401.
        username_ok = hmac.compare_digest(username.encode("utf-8"),
                                          DASHBOARD_USERNAME.encode("utf-8"))
        password_ok = hmac.compare_digest(password.encode("utf-8"),
                                          DASHBOARD_PASSWORD.encode("utf-8"))
        return username_ok and password_ok

    def do_GET(self) -> None:
        if not self._authorized():
            self._unauthorized()
            return

        path = urlsplit(self.path).path

        if path == "/":
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
            return

        if path == "/api/dashboard":
            try:
                body = json.dumps(
                    read_dashboard_data(),
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            except Exception:
                # Keep internal report details out of HTTP error responses.
                self._send(
                    500,
                    "application/json; charset=utf-8",
                    b'{"error":"Dashboard data could not be read."}',
                )
            return

        if path == "/api/scan":
            self._json(200, read_scan_status())
            return

        if path == "/api/snapshot":
            self._snapshot(urlsplit(self.path).query)
            return

        self._send(404, "text/plain; charset=utf-8", b"Not found.\n")

    def _json(self, code: int, data: dict) -> None:
        self._send(code, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _snapshot(self, query: str) -> None:
        """Latest stored thumbnail of one camera (written by the reporter)."""
        params = dict(part.partition("=")[::2] for part in query.split("&") if part)
        nvr_id, channel = params.get("nvr", ""), params.get("ch", "")
        if not notes_store.valid_target(nvr_id, channel) or not channel:
            self._send(400, "text/plain; charset=utf-8", b"Bad request.\n")
            return
        path = SNAPSHOT_DIR / nvr_id / f"{channel}.jpg"
        try:
            body = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        except OSError:
            body = None
        if not body or not body.startswith(b"\xff\xd8") or len(body) > 5_000_000:
            self._send(404, "text/plain; charset=utf-8", b"No picture.\n")
            return
        self._send(200, "image/jpeg", body)

    def do_POST(self) -> None:
        if not self._authorized():
            self._unauthorized()
            return
        route = urlsplit(self.path).path
        if route not in {"/api/scan", "/api/notes"}:
            self._send(404, "text/plain; charset=utf-8", b"Not found.\n")
            return
        # Cross-site protection: browsers cannot send this header or a JSON
        # body cross-origin without a CORS preflight, which is never granted.
        origin = self.headers.get("Origin")
        if (self.headers.get("X-NVR-Track") != route.rsplit("/", 1)[-1]
                or not self.headers.get("Content-Type", "").startswith("application/json")
                or (origin and origin != f"http://{self.headers.get('Host', '')}")):
            self._json(403, {"error": "Request refused."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "Invalid request."})
            return
        if route == "/api/notes":
            try:
                code, data = save_note(body, "Dashboard")
            except OSError:
                code, data = 503, {"error": "The note could not be saved."}
            self._json(code, data)
            return
        try:
            code, data = request_scan(body.get("target"))
        except OSError:
            code, data = 503, {"error": "The scan request could not be saved."}
        self._json(code, data)


def main() -> int:
    if not DASHBOARD_USERNAME or not DASHBOARD_PASSWORD:
        raise SystemExit(
            "Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD in the environment."
        )

    if len(DASHBOARD_PASSWORD) < 16:
        raise SystemExit(
            "DASHBOARD_PASSWORD must be at least 16 characters long."
        )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)

    print(f"NVR dashboard listening on {HOST}:{PORT}")
    print(f"Reading reports from {REPORTS_DIR}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())