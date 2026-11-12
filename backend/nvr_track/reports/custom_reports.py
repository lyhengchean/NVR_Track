"""Report builder: pick one or more data sets, NVRs, dates and, per data set,
columns, conditions, sort and limit, like database queries; preview them,
save them on the server as one report under any name (state/custom_reports)
and download it later as PDF (one section per data set) or CSV (a ZIP with one
CSV per data set when there are several).

Built only from data NVR Track already has: saved scan reports, recorded
real-time events and repair notes. No NVR is contacted. Failed, disabled or
unverified checks stay "Not verified" / blank; they are never counted as
recording or online, and no footage in a check window is not "never recorded".
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import threading
import uuid
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nvr_track.nvr import events as event_store
from nvr_track.monitoring import notes as notes_store
from nvr_track.monitoring.camera_condition import CONDITIONS, calendar_facts
from nvr_track.nvr.storage import ABSENT_DISK_STATES, NORMAL_DISK_STATES
from nvr_track.bot.telegram_views import LIVE_LABEL, PICTURE_LABEL
from nvr_track.core.paths import PROJECT_ROOT as ROOT
from nvr_track.core.spreadsheet import csv_safe

LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")
SCHEMA = 2                   # 2: several sections per saved report; 1 (one table) is still read.
MAX_RANGE_DAYS = 366
MAX_ROWS = 50_000            # Per section.
MAX_TOTAL_ROWS = 150_000     # Per saved report.
PREVIEW_ROWS = 200
PDF_ROWS = 3000
MAX_SAVED = 300
MAX_CONDITIONS = 6
NAME_CHARS = 80
REPORT_NAME = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9_-]{0,63}?)_"
                         r"(?P<stamp>\d{8}T\d{6}(?:\d{1,6})?[+-]\d{4})\.json$")
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SAVED_ID = re.compile(r"[0-9a-f]{12}")
EVIDENCE = {"RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY"}
CHECKED = EVIDENCE | {"NO_RECENT_RECORDING_EVIDENCE"}

HEALTH = {"HEALTHY": "Healthy", "WARNING": "Warning", "CRITICAL": "Critical", "UNKNOWN": "Unknown",
          "ERROR": "Report failed"}
LIVE_VALUES = [LIVE_LABEL[k] for k in ("ONLINE", "OFFLINE", "UNKNOWN", "NOT_RETURNED")]
CONDITION_VALUES = [label for label, _, _ in CONDITIONS.values()]
RECORDING_VALUES = ["Recorded", "No footage in window", "Not verified", "Not enabled"]
HEALTH_VALUES = ["Healthy", "Warning", "Critical", "Unknown"]
PICTURE_VALUES = list(dict.fromkeys(PICTURE_LABEL.values()))
SMART_VALUES = ["Healthy", "Warning", "Critical", "Not verified", "Not enabled"]
CHANGE_LABEL = {"added": "Added", "removed": "Removed", "renamed": "Renamed", "ip_changed": "IP changed"}
NOTE_LABEL = {"note": "Note", "known": "Known issue", "fixed": "Fixed"}
EVENT_VALUES = sorted({label for label, _, _ in event_store.EVENT_TYPES.values()})

OPS = {"is": "=", "is_not": "≠", "contains": "contains", "not_contains": "does not contain",
       "gt": ">", "ge": "≥", "lt": "<", "le": "≤", "empty": "is empty", "not_empty": "is not empty"}


def _col(key: str, label: str, kind: str = "text", default: bool = True, values: list | tuple = ()) -> dict:
    return {"key": key, "label": label, "kind": kind, "default": default, "values": list(values)}


def _nvr_cols() -> list[dict]:
    return [_col("nvr", "NVR"), _col("nvr_name", "NVR name")]


# mode "as_of": the state on one date (the newest report up to its end);
# mode "range": everything between two dates (inclusive, local time).
DATASETS: dict[str, dict] = {
    "cameras": {
        "label": "Cameras", "mode": "as_of", "sort": ("nvr", False),
        "help": "Every camera as on a date: live status, condition, recording, footage kept, picture.",
        "columns": _nvr_cols() + [
            _col("channel", "Channel", "number"), _col("camera", "Camera"),
            _col("ip", "Camera IP", default=False),
            _col("live", "Live", "enum", values=LIVE_VALUES),
            _col("condition", "Condition", "enum", values=CONDITION_VALUES),
            _col("recording", "Recording", "enum", values=RECORDING_VALUES),
            _col("kept_days", "Footage kept (days)", "number"),
            _col("missing_days", "Days without footage", "number", default=False),
            _col("last_footage", "Last day with footage", default=False),
            _col("picture", "Picture", "enum", default=False, values=PICTURE_VALUES),
            _col("known_issue", "Known issue", default=False),
            _col("detail", "Detail", default=False),
            _col("checked_at", "Report time", "time"),
        ]},
    "camera_uptime": {
        "label": "Camera uptime", "mode": "range", "sort": ("online_pct", False),
        "help": "Per camera over a period: online %, recording %, times offline, offline/online changes.",
        "columns": _nvr_cols() + [
            _col("channel", "Channel", "number"), _col("camera", "Camera"),
            _col("samples", "Scans", "number"),
            _col("online_pct", "Online %", "percent"),
            _col("offline_scans", "Scans offline", "number"),
            _col("recording_pct", "Recording %", "percent"),
            _col("recording_checks", "Recording checks", "number", default=False),
            _col("changes", "Offline/online changes", "number"),
            _col("first_offline", "First seen offline", "time", default=False),
            _col("last_offline", "Last seen offline", "time"),
            _col("last_live", "Last live status", "enum", values=LIVE_VALUES),
        ]},
    "nvr_health": {
        "label": "NVR health", "mode": "range", "sort": ("nvr", False),
        "help": "Per NVR over a period: reachable %, healthy/warning/critical scans, restarts, cameras offline.",
        "columns": _nvr_cols() + [
            _col("reports", "Scans", "number"),
            _col("reachable_pct", "Reachable %", "percent"),
            _col("healthy", "Healthy scans", "number"),
            _col("warning", "Warning scans", "number"),
            _col("critical", "Critical scans", "number"),
            _col("restarts", "Restarts", "number"),
            _col("cameras", "Cameras", "number"),
            _col("avg_offline", "Avg. cameras offline", "number"),
            _col("max_offline", "Most cameras offline", "number"),
            _col("online_pct", "Camera online %", "percent", default=False),
            _col("recording_pct", "Camera recording %", "percent", default=False),
            _col("last_status", "Last status", "enum", values=HEALTH_VALUES),
            _col("last_storage", "Last storage", "enum", default=False, values=HEALTH_VALUES),
            _col("first_report", "First scan", "time", default=False),
            _col("last_report", "Last scan", "time"),
        ]},
    "scans": {
        "label": "Scan log", "mode": "range", "sort": ("time", True),
        "help": "One row per NVR scan: status, cameras online/offline, recorded, storage, restart.",
        "columns": [_col("time", "Time", "time")] + _nvr_cols() + [
            _col("status", "Status", "enum", values=HEALTH_VALUES),
            _col("online", "Online", "number"), _col("offline", "Offline", "number"),
            _col("unknown", "Unknown", "number", default=False),
            _col("recorded", "Cameras recorded", "number"),
            _col("no_footage", "No footage in window", "number"),
            _col("storage", "Storage", "enum", values=HEALTH_VALUES),
            _col("restart", "Restarted", "enum", values=["Yes", "No"]),
            _col("findings", "Findings", default=False),
        ]},
    "disks": {
        "label": "Disks", "mode": "as_of", "sort": ("nvr", False),
        "help": "Every disk as on a date: status, capacity, free space, SMART, temperature.",
        "columns": _nvr_cols() + [
            _col("hdd", "HDD"), _col("status", "Status"),
            _col("problem", "Needs attention", "enum", values=["Yes", "No"]),
            _col("capacity_gb", "Capacity (GB)", "number"), _col("free_gb", "Free (GB)", "number"),
            _col("used_pct", "Used %", "percent"),
            _col("smart", "SMART", "enum", values=SMART_VALUES),
            _col("temperature", "Temp. (°C)", "number"),
            _col("power_on_days", "Power-on days", "number", default=False),
            _col("reallocated", "Reallocated sectors", "number", default=False),
            _col("checked_at", "Report time", "time"),
        ]},
    "events": {
        "label": "Real-time events", "mode": "range", "sort": ("time", True),
        "help": "Alarms from the NVRs' event streams: video loss, disk errors, network, illegal logins.",
        "columns": [_col("time", "Time", "time")] + _nvr_cols() + [
            _col("channel", "Channel", "number"),
            _col("event", "Event", "enum", values=EVENT_VALUES),
            _col("severity", "Severity", "enum", values=["Critical", "Warning"]),
            _col("description", "Details", default=False),
        ]},
    "changes": {
        "label": "Camera changes", "mode": "range", "sort": ("time", True),
        "help": "Cameras added, removed, renamed or with a new IP, as found by the scans.",
        "columns": [_col("time", "Found at", "time")] + _nvr_cols() + [
            _col("channel", "Channel", "number"),
            _col("change", "Change", "enum", values=list(CHANGE_LABEL.values())),
            _col("old", "Before"), _col("new", "After"),
        ]},
    "notes": {
        "label": "Repair notes", "mode": "range", "sort": ("time", True),
        "help": "Notes, known issues and fixes written on the dashboard or in Telegram.",
        "columns": [_col("time", "Time", "time")] + _nvr_cols() + [
            _col("channel", "Channel", "number"),
            _col("kind", "Type", "enum", values=list(NOTE_LABEL.values())),
            _col("text", "Text"), _col("by", "By"),
        ]},
}


# ---------- small helpers ----------

def saved_dir() -> Path:
    return Path(os.environ.get("CUSTOM_REPORTS_DIR", str(ROOT / "state" / "custom_reports")))


def today_local() -> date:
    return datetime.now(LOCAL_TZ).date()


def _when(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def _stamp(text: str) -> datetime | None:
    for pattern in ("%Y%m%dT%H%M%S%f%z", "%Y%m%dT%H%M%S%z"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _local(value: object) -> str | None:
    moment = value if isinstance(value, datetime) else _when(value)
    return moment.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") if moment else None


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), LOCAL_TZ)


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _channel(value: object) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() and len(text) <= 6 else None


def _recording(status: object) -> str:
    if status in EVIDENCE:
        return "Recorded"
    if status == "NO_RECENT_RECORDING_EVIDENCE":
        return "No footage in window"
    return "Not enabled" if status == "NOT_CONFIGURED" else "Not verified"


def clean_name(value: object) -> str:
    text = " ".join("".join(ch for ch in str(value or "") if ch.isprintable()).split())
    return text[:NAME_CHARS]


# ---------- the query ----------

SECTION_KEYS = ("dataset", "columns", "conditions", "sort", "limit")


def normalize(raw: object, today: date | None = None) -> tuple[dict | None, str | None]:
    """A checked query from the page, or (None, a message for the user).

    {"nvrs", "date_from", "date_to", "sections": [{"dataset", "columns",
    "conditions", "sort", "limit"}, ...]}; NVRs and dates are shared, "on a
    date" sections use the To date. The one-table form saved before
    (a "dataset" key at the top) is accepted too."""
    today = today or today_local()
    raw = raw if isinstance(raw, dict) else {}
    if "sections" not in raw and "dataset" in raw:
        raw = {**raw, "sections": [{k: raw.get(k) for k in SECTION_KEYS}]}
    sections = raw.get("sections")
    if not isinstance(sections, list) or not sections:
        return None, "Choose what to report on."
    if len(sections) > len(DATASETS) or not all(isinstance(x, dict) and x.get("dataset") in DATASETS
                                                for x in sections):
        return None, "Choose what to report on."
    if len({x["dataset"] for x in sections}) != len(sections):
        return None, "Each data set can be chosen once."
    nvrs = raw.get("nvrs") or []
    if not isinstance(nvrs, list) or len(nvrs) > 100 or not all(
            isinstance(n, str) and DEVICE_ID.fullmatch(n) for n in nvrs):
        return None, "Choose NVRs from the list."
    period = any(DATASETS[x["dataset"]]["mode"] == "range" for x in sections)
    try:
        date_to = date.fromisoformat(str(raw.get("date_to") or today.isoformat()))
        date_from = (date.fromisoformat(str(raw.get("date_from") or (date_to - timedelta(days=6)).isoformat()))
                     if period else date_to)
    except ValueError:
        return None, "Dates must be like 2026-11-06."
    if date_to > today:
        return None, "The end date cannot be in the future."
    if date_from > date_to:
        return None, "The start date is after the end date."
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        return None, f"Choose at most {MAX_RANGE_DAYS} days."
    checked = []
    for section in sections:
        clean, error = _normalize_section(section)
        if error:
            return None, (f"{DATASETS[section['dataset']]['label']}: {error}" if len(sections) > 1 else error)
        checked.append(clean)
    return {"nvrs": sorted(set(nvrs)), "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
            "sections": checked}, None


def _normalize_section(raw: dict) -> tuple[dict | None, str | None]:
    key = raw["dataset"]
    dataset = DATASETS[key]
    columns = {c["key"]: c for c in dataset["columns"]}
    chosen = raw.get("columns") or [c["key"] for c in dataset["columns"] if c["default"]]
    if not isinstance(chosen, list) or not chosen or not all(isinstance(c, str) and c in columns for c in chosen):
        return None, "Choose at least one column."
    conditions = []
    for item in raw.get("conditions") or []:
        if not isinstance(item, dict) or item.get("column") not in columns or item.get("op") not in OPS:
            return None, "Each condition needs a column and a comparison."
        value = str(item.get("value") if item.get("value") is not None else "").strip()[:100]
        if item["op"] not in {"empty", "not_empty"} and value == "":
            return None, f"Give a value for “{columns[item['column']]['label']}”."
        if item["op"] in {"gt", "ge", "lt", "le"} and columns[item["column"]]["kind"] in {"number", "percent"}:
            try:
                float(value)
            except ValueError:
                return None, f"“{columns[item['column']]['label']}” needs a number."
        conditions.append({"column": item["column"], "op": item["op"], "value": value})
    if len(conditions) > MAX_CONDITIONS:
        return None, f"Use at most {MAX_CONDITIONS} conditions."
    sort = raw.get("sort") if isinstance(raw.get("sort"), dict) else {}
    sort_column = sort.get("column") if sort.get("column") in columns else dataset["sort"][0]
    descending = bool(sort.get("desc")) if sort.get("column") in columns else dataset["sort"][1]
    limit = raw.get("limit")
    if limit in (None, ""):
        limit = None
    elif type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        return None, f"Row limit must be 1 to {MAX_ROWS}."
    return {"dataset": key, "columns": list(dict.fromkeys(chosen)), "conditions": conditions,
            "sort": {"column": sort_column, "desc": descending}, "limit": limit}, None


def flat(spec: dict, section: dict) -> dict:
    """One section with the shared NVRs and dates ("on a date": the To date)."""
    as_of = DATASETS[section["dataset"]]["mode"] == "as_of"
    return {**section, "nvrs": spec["nvrs"], "date_to": spec["date_to"],
            "date_from": spec["date_to"] if as_of else spec["date_from"]}


def query_text(spec: dict) -> str:
    """One section's query in words, like SQL, for the page and the PDF."""
    dataset = DATASETS[spec["dataset"]]
    labels = {c["key"]: c["label"] for c in dataset["columns"]}
    kinds = {c["key"]: c["kind"] for c in dataset["columns"]}
    where = []
    if spec["nvrs"]:
        where.append("NVR IN (" + ", ".join(spec["nvrs"]) + ")")
    for c in spec["conditions"]:
        value = ("" if c["op"] in {"empty", "not_empty"} else f" {c['value']}"
                 if kinds[c["column"]] in {"number", "percent"} else f' "{c["value"]}"')
        where.append(f"{labels[c['column']]} {OPS[c['op']]}{value}")
    when = (f"ON {spec['date_to']}" if dataset["mode"] == "as_of"
            else f"FROM {spec['date_from']} TO {spec['date_to']}")
    text = f"SELECT {', '.join(labels[k] for k in spec['columns'])} FROM {dataset['label']} {when}"
    if where:
        text += " WHERE " + " AND ".join(where)
    text += f" ORDER BY {labels[spec['sort']['column']]} {'DESC' if spec['sort']['desc'] else 'ASC'}"
    return text + (f" LIMIT {spec['limit']}" if spec["limit"] else "")


def _matches(value: object, op: str, wanted: str, kind: str) -> bool:
    empty = value is None or value == ""
    if op == "empty":
        return empty
    if op == "not_empty":
        return not empty
    if kind in {"number", "percent"} and op in {"gt", "ge", "lt", "le", "is", "is_not"}:
        try:
            target = float(wanted)
        except ValueError:
            target = None
        if target is not None:
            if empty or not isinstance(value, (int, float)):
                return op == "is_not"
            return {"gt": value > target, "ge": value >= target, "lt": value < target, "le": value <= target,
                    "is": value == target, "is_not": value != target}[op]
    text, wanted = ("" if empty else str(value)).casefold(), wanted.casefold()
    if kind == "time" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", wanted) and op in {"is", "is_not", "gt", "ge", "lt", "le"}:
        text = text[:10]  # A date alone means the whole day: "on or before 2026-11-05" includes 14:00.
    if op == "is":
        return text == wanted
    if op == "is_not":
        return text != wanted
    if op == "contains":
        return wanted in text
    if op == "not_contains":
        return wanted not in text
    if empty:
        return False
    return {"gt": text > wanted, "ge": text >= wanted, "lt": text < wanted, "le": text <= wanted}[op]


_build_lock = threading.Lock()


def build(spec: dict, reports_dir: Path, today: date | None = None) -> dict:
    """Run every section: {"sections": [{"dataset", "label", "mode", "columns",
    "rows" (lists), "total", "truncated", "meta", "query"}, ...]}."""
    return {"sections": [build_section(flat(spec, section), reports_dir, today) for section in spec["sections"]]}


def build_section(spec: dict, reports_dir: Path, today: date | None = None) -> dict:
    dataset = DATASETS[spec["dataset"]]
    kinds = {c["key"]: c["kind"] for c in dataset["columns"]}
    with _build_lock:  # One at a time: a year of reports takes a while the first time.
        rows, meta = BUILDERS[spec["dataset"]](spec, reports_dir, today or today_local())
    wanted = set(spec["nvrs"])
    rows = [r for r in rows if not wanted or r.get("nvr") in wanted]
    for c in spec["conditions"]:
        rows = [r for r in rows if _matches(r.get(c["column"]), c["op"], c["value"], kinds[c["column"]])]
    key, desc = spec["sort"]["column"], spec["sort"]["desc"]
    numeric = kinds[key] in {"number", "percent"}

    def order(row):
        value = row.get(key)
        missing = value is None or value == ""   # Blank rows last, in both directions.
        if numeric:
            value = float(value) if isinstance(value, (int, float)) else 0.0
        else:
            value = str(value or "").casefold()
        return (not missing if desc else missing, value, str(row.get("nvr") or ""),
                row.get("channel") if isinstance(row.get("channel"), int) else 0)
    rows.sort(key=order, reverse=desc)
    total = len(rows)
    limit = min(spec["limit"] or MAX_ROWS, MAX_ROWS)
    columns = [{"key": k, "label": c["label"], "kind": c["kind"]}
               for k in spec["columns"] for c in dataset["columns"] if c["key"] == k]
    return {"dataset": spec["dataset"], "label": dataset["label"], "mode": dataset["mode"],
            "date_from": spec["date_from"], "date_to": spec["date_to"],
            "columns": columns, "rows": [[r.get(k) for k in spec["columns"]] for r in rows[:limit]],
            "total": total, "truncated": total > limit, "meta": meta, "query": query_text(spec)}


# ---------- saved scan reports (compact summaries, cached) ----------

_cache: dict[str, tuple[int, int, dict | None]] = {}
_cache_lock = threading.Lock()


def _files(reports_dir: Path, start: datetime | None, end: datetime) -> list[tuple[datetime, str, Path]]:
    found = []
    try:
        paths = list(reports_dir.glob("*_*.json"))
    except OSError:
        return found
    for path in paths:
        match = REPORT_NAME.match(path.name)
        moment = _stamp(match["stamp"]) if match else None
        if moment is None or moment >= end or (start is not None and moment < start) or path.is_symlink():
            continue
        found.append((moment, match["id"], path))
    found.sort(key=lambda item: (item[0], item[2].name))
    return found


def _summarize(report: object, nvr_id: str) -> dict | None:
    if not isinstance(report, dict) or (report.get("nvr") or {}).get("id") != nvr_id:
        return None
    at = _when(report.get("check_completed_at") or report.get("check_started_at"))
    if at is None:
        return None
    overall = report.get("overall") if isinstance(report.get("overall"), dict) else {}
    status = report.get("channel_status") if isinstance(report.get("channel_status"), dict) else {}
    channels = []
    for ch in (report.get("channel_inventory") or {}).get("channels") or []:
        if isinstance(ch, dict):
            channels.append([str(ch.get("channel_id", "")), str(ch.get("name") or "")[:80],
                             str(ch.get("reported_status") or "UNKNOWN"),
                             str((ch.get("recording_result") or {}).get("status") or "NOT_CHECKED")])

    def count(key, state):
        value = status.get(key)
        return value if type(value) is int else sum(c[2] == state for c in channels)
    changes = [{"type": c.get("type"), "channel": str(c.get("channel_id") or ""), "old": str(c.get("old") or "")[:80],
                "new": str(c.get("new") or "")[:80]}
               for c in (report.get("inventory_changes") or {}).get("changes") or []
               if isinstance(c, dict) and c.get("type") in CHANGE_LABEL]
    findings = [str(x)[:200] for x in (overall.get("critical_reasons") or []) + (overall.get("warnings") or [])]
    return {"nvr": nvr_id, "name": str((report.get("nvr") or {}).get("name") or "")[:80], "at": at.isoformat(),
            "reachable": (report.get("device_info") or {}).get("status") == "DEVICE_INFO_RETRIEVED",
            "overall": str(overall.get("status") or "UNKNOWN"), "findings": findings[:8],
            "online": count("online_count", "ONLINE"), "offline": count("offline_count", "OFFLINE"),
            "unknown": count("unknown_count", "UNKNOWN"),
            "storage": str((report.get("storage") or {}).get("status") or "UNKNOWN"),
            "restart": (report.get("restart") or {}).get("detected") is True,
            "changes": changes[:100], "channels": channels}


def _summary(path: Path, nvr_id: str) -> dict | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    with _cache_lock:
        hit = _cache.get(path.name)
    if hit and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
        return hit[2]
    try:
        with path.open("r", encoding="utf-8") as handle:
            summary = _summarize(json.load(handle), nvr_id)
    except (OSError, ValueError, RecursionError):
        summary = None
    with _cache_lock:
        if len(_cache) > 200_000:
            _cache.clear()
        _cache[path.name] = (stat.st_mtime_ns, stat.st_size, summary)
    return summary


def _period(spec: dict, reports_dir: Path) -> tuple[list[dict], dict]:
    start = _midnight(date.fromisoformat(spec["date_from"]))
    end = _midnight(date.fromisoformat(spec["date_to"]) + timedelta(days=1))
    wanted = set(spec["nvrs"])
    summaries = [s for _, nvr_id, path in _files(reports_dir, start, end)
                 if not wanted or nvr_id in wanted
                 for s in [_summary(path, nvr_id)] if s is not None]
    meta = {"source": f"{len(summaries)} saved scan reports",
            "first": _local(summaries[0]["at"]) if summaries else None,
            "last": _local(summaries[-1]["at"]) if summaries else None}
    return summaries, meta


def nvr_names(reports_dir: Path) -> dict[str, str]:
    """NVR id -> display name, from the newest report of each NVR."""
    newest: dict[str, Path] = {}
    for _, nvr_id, path in _files(reports_dir, None, datetime.now(timezone.utc) + timedelta(days=1)):
        newest[nvr_id] = path
    names = {}
    for nvr_id, path in sorted(newest.items()):
        summary = _summary(path, nvr_id)
        names[nvr_id] = summary["name"] if summary else ""
    return names


def oldest_date(reports_dir: Path) -> str | None:
    files = _files(reports_dir, None, datetime.now(timezone.utc) + timedelta(days=1))
    return files[0][0].astimezone(LOCAL_TZ).date().isoformat() if files else None


# ---------- data sets ----------

def _as_of_nvrs(spec: dict, reports_dir: Path, today: date) -> tuple[list[dict], dict]:
    """Dashboard-shaped NVRs on the end date: today = what the dashboard shows
    now (live checks, known issues); earlier = the newest report up to that day."""
    from nvr_track.web import dashboard  # The dashboard imports this module.
    if date.fromisoformat(spec["date_to"]) >= today:
        nvrs = [n for n in dashboard.read_dashboard_data()["nvrs"] if n.get("channels") or n.get("disks")]
        return nvrs, {"source": "the dashboard now (latest scan of each NVR, live checks where enabled)"}
    end = _midnight(date.fromisoformat(spec["date_to"]) + timedelta(days=1))
    newest: dict[str, Path] = {}
    for _, nvr_id, path in _files(reports_dir, None, end):
        newest[nvr_id] = path
    nvrs = []
    for nvr_id, path in sorted(newest.items()):
        if spec["nvrs"] and nvr_id not in spec["nvrs"]:
            continue
        loaded = dashboard._load_report(path)
        if loaded and loaded[0] == nvr_id:
            nvrs.append(dashboard._public_report(loaded[2], path, None))
    return nvrs, {"source": f"the newest scan of each NVR up to {spec['date_to']}"}


def _cameras(spec: dict, reports_dir: Path, today: date) -> tuple[list[dict], dict]:
    nvrs, meta = _as_of_nvrs(spec, reports_dir, today)
    rows = []
    for nvr in nvrs:
        for ch in nvr.get("channels") or []:
            cal = calendar_facts(ch.get("retention_result"))
            picture = (ch.get("snapshot_result") or {}).get("status")
            rows.append({
                "nvr": nvr["id"], "nvr_name": nvr.get("name") or "", "channel": _channel(ch.get("channel_id")),
                "camera": ch.get("name") or "", "ip": ch.get("camera_ip") or "",
                "live": LIVE_LABEL.get(ch.get("reported_status"), str(ch.get("reported_status") or "").title()),
                "condition": (ch.get("condition") or {}).get("label"),
                "recording": _recording((ch.get("recording_result") or {}).get("status")),
                "kept_days": (min(cal["history_days"], cal["window"])
                              if cal["known"] and cal["history_days"] > 0 and type(cal["window"]) is int else None),
                "missing_days": cal["missing_days"] if cal["known"] else None,
                "last_footage": cal["last_day"] if cal["known"] else None,
                "picture": (PICTURE_LABEL.get(picture) if picture not in {None, "PICTURE_NOT_CONFIGURED"} else None),
                "known_issue": (ch.get("known_issue") or {}).get("reason") or None,
                "detail": (ch.get("condition") or {}).get("detail"),
                "checked_at": _local(nvr.get("last_check")),
            })
    return rows, meta


def _disks(spec: dict, reports_dir: Path, today: date) -> tuple[list[dict], dict]:
    nvrs, meta = _as_of_nvrs(spec, reports_dir, today)
    smart_label = {"HEALTHY": "Healthy", "WARNING": "Warning", "CRITICAL": "Critical",
                   "UNVERIFIED": "Not verified", "NOT_CONFIGURED": "Not enabled"}
    rows = []
    for nvr in nvrs:
        for disk in nvr.get("disks") or []:
            status = str(disk.get("reported_status") or "").strip().lower()
            cap, free = disk.get("capacity_mb"), disk.get("free_space_mb")
            numbers = isinstance(cap, (int, float)) and isinstance(free, (int, float)) and cap > 0
            smart = disk.get("smart") or {}
            attention = (status not in NORMAL_DISK_STATES | ABSENT_DISK_STATES
                         or smart.get("status") in {"WARNING", "CRITICAL"})
            rows.append({
                "nvr": nvr["id"], "nvr_name": nvr.get("name") or "", "hdd": disk.get("id") or "",
                "status": status or "unknown", "problem": "Yes" if attention else "No",
                "capacity_gb": round(cap / 1024) if numbers else None,
                "free_gb": round(free / 1024) if numbers else None,
                "used_pct": round(100.0 * (cap - free) / cap, 1) if numbers and free <= cap else None,
                "smart": smart_label.get(smart.get("status"), "Not enabled") if smart else "Not enabled",
                "temperature": smart.get("temperature_c"), "power_on_days": smart.get("power_on_days"),
                "reallocated": smart.get("reallocated"), "checked_at": _local(nvr.get("last_check")),
            })
    return rows, meta


def _camera_uptime(spec: dict, reports_dir: Path, today: date) -> tuple[list[dict], dict]:
    summaries, meta = _period(spec, reports_dir)
    cams: dict[tuple[str, str], dict] = {}
    for s in summaries:
        for cid, name, live, rec in s["channels"]:
            cam = cams.setdefault((s["nvr"], cid), {
                "nvr": s["nvr"], "nvr_name": s["name"], "channel": _channel(cid), "camera": name,
                "samples": 0, "_known": 0, "_online": 0, "offline_scans": 0, "_checked": 0, "_recorded": 0,
                "changes": 0, "first_offline": None, "last_offline": None, "_last": None, "last_live": None})
            cam.update(nvr_name=s["name"] or cam["nvr_name"], camera=name or cam["camera"])
            cam["samples"] += 1
            cam["last_live"] = LIVE_LABEL.get(live, live.title())
            if live in {"ONLINE", "OFFLINE"}:
                cam["_known"] += 1
                cam["_online"] += live == "ONLINE"
                if live == "OFFLINE":
                    cam["offline_scans"] += 1
                    cam["first_offline"] = cam["first_offline"] or _local(s["at"])
                    cam["last_offline"] = _local(s["at"])
                if cam["_last"] is not None and cam["_last"] != live:
                    cam["changes"] += 1
                cam["_last"] = live
            if rec in CHECKED:
                cam["_checked"] += 1
                cam["_recorded"] += rec in EVIDENCE
    rows = []
    for cam in cams.values():
        cam["online_pct"] = _pct(cam["_online"], cam["_known"])
        cam["recording_pct"] = _pct(cam["_recorded"], cam["_checked"])
        cam["recording_checks"] = cam["_checked"]
        rows.append({k: v for k, v in cam.items() if not k.startswith("_")})
    return rows, meta


def _nvr_health(spec: dict, reports_dir: Path, today: date) -> tuple[list[dict], dict]:
    summaries, meta = _period(spec, reports_dir)
    groups: dict[str, list[dict]] = {}
    for s in summaries:
        groups.setdefault(s["nvr"], []).append(s)
    rows = []
    for nvr_id, items in groups.items():
        offline = [s["offline"] for s in items]
        known = online = checked = recorded = 0
        for s in items:
            for _, _, live, rec in s["channels"]:
                known += live in {"ONLINE", "OFFLINE"}
                online += live == "ONLINE"
                checked += rec in CHECKED
                recorded += rec in EVIDENCE
        rows.append({
            "nvr": nvr_id, "nvr_name": items[-1]["name"], "reports": len(items),
            "reachable_pct": _pct(sum(s["reachable"] for s in items), len(items)),
            "healthy": sum(s["overall"] == "HEALTHY" for s in items),
            "warning": sum(s["overall"] == "WARNING" for s in items),
            "critical": sum(s["overall"] == "CRITICAL" for s in items),
            "restarts": sum(s["restart"] for s in items),
            "cameras": len(items[-1]["channels"]),
            "avg_offline": round(sum(offline) / len(offline), 1), "max_offline": max(offline),
            "online_pct": _pct(online, known), "recording_pct": _pct(recorded, checked),
            "last_status": HEALTH.get(items[-1]["overall"], items[-1]["overall"].title()),
            "last_storage": HEALTH.get(items[-1]["storage"], items[-1]["storage"].title()),
            "first_report": _local(items[0]["at"]), "last_report": _local(items[-1]["at"]),
        })
    return rows, meta


def _scans(spec: dict, reports_dir: Path, today: date) -> tuple[list[dict], dict]:
    summaries, meta = _period(spec, reports_dir)
    return [{"time": _local(s["at"]), "nvr": s["nvr"], "nvr_name": s["name"],
             "status": HEALTH.get(s["overall"], s["overall"].title()),
             "online": s["online"], "offline": s["offline"], "unknown": s["unknown"],
             "recorded": sum(c[3] in EVIDENCE for c in s["channels"]),
             "no_footage": sum(c[3] == "NO_RECENT_RECORDING_EVIDENCE" for c in s["channels"]),
             "storage": HEALTH.get(s["storage"], s["storage"].title()),
             "restart": "Yes" if s["restart"] else "No", "findings": "; ".join(s["findings"]) or None}
            for s in summaries], meta


def _changes(spec: dict, reports_dir: Path, today: date) -> tuple[list[dict], dict]:
    summaries, meta = _period(spec, reports_dir)
    return [{"time": _local(s["at"]), "nvr": s["nvr"], "nvr_name": s["name"], "channel": _channel(c["channel"]),
             "change": CHANGE_LABEL[c["type"]], "old": c["old"] or None, "new": c["new"] or None}
            for s in summaries for c in s["changes"]], meta


def _events(spec: dict, reports_dir: Path, today: date) -> tuple[list[dict], dict]:
    start = _midnight(date.fromisoformat(spec["date_from"]))
    end = _midnight(date.fromisoformat(spec["date_to"]) + timedelta(days=1))
    names = nvr_names(reports_dir)
    rows = []
    for name in ("events.jsonl.1", "events.jsonl"):
        try:
            text = (event_store.events_dir() / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                line = event_store._valid(json.loads(raw))
            except (ValueError, TypeError):
                continue
            moment = _when(line["at"]) if line else None
            if moment is None or not start <= moment < end:
                continue
            rows.append({"time": _local(moment), "nvr": line["nvr"], "nvr_name": names.get(line["nvr"], ""),
                         "channel": _channel(line["channel"]), "event": line["label"],
                         "severity": line["severity"].title() or None, "description": line["description"] or None})
    return rows, {"source": "events recorded by the real-time listener (nvr-events)"}


def _notes(spec: dict, reports_dir: Path, today: date) -> tuple[list[dict], dict]:
    start = _midnight(date.fromisoformat(spec["date_from"]))
    end = _midnight(date.fromisoformat(spec["date_to"]) + timedelta(days=1))
    names = nvr_names(reports_dir)
    rows = []
    for note in notes_store.load().get("log") or []:
        moment = _when(note.get("at"))
        if moment is None or not start <= moment < end:
            continue
        rows.append({"time": _local(moment), "nvr": note.get("nvr"), "nvr_name": names.get(note.get("nvr"), ""),
                     "channel": _channel(note.get("channel")),
                     "kind": NOTE_LABEL.get(note.get("kind"), "Note"), "text": note.get("text") or None,
                     "by": note.get("by") or None})
    return rows, {"source": "the repair log (dashboard and Telegram notes)"}


BUILDERS = {"cameras": _cameras, "camera_uptime": _camera_uptime, "nvr_health": _nvr_health, "scans": _scans,
            "disks": _disks, "events": _events, "changes": _changes, "notes": _notes}


def options(reports_dir: Path) -> dict:
    """Everything the builder page needs to offer choices."""
    return {"datasets": [{"key": key, "label": d["label"], "mode": d["mode"], "help": d["help"],
                          "sort": {"column": d["sort"][0], "desc": d["sort"][1]}, "columns": d["columns"]}
                         for key, d in DATASETS.items()],
            "ops": OPS, "nvrs": [{"id": k, "name": v} for k, v in nvr_names(reports_dir).items()],
            "today": today_local().isoformat(), "oldest": oldest_date(reports_dir),
            "limits": {"max_rows": MAX_ROWS, "max_total_rows": MAX_TOTAL_ROWS, "preview_rows": PREVIEW_ROWS, "max_days": MAX_RANGE_DAYS,
                       "max_conditions": MAX_CONDITIONS, "name_chars": NAME_CHARS, "pdf_rows": PDF_ROWS}}


# ---------- saved reports ----------

_save_lock = threading.Lock()


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".save",
                                     suffix=".tmp", delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _item(doc: dict) -> dict:
    spec = doc.get("spec") or {}
    sections = doc["sections"]
    return {"id": doc["id"], "name": doc.get("name") or "", "created_at": doc.get("created_at"),
            "datasets": [x.get("dataset") for x in sections], "labels": [x.get("label") or "" for x in sections],
            "modes": [x.get("mode") for x in sections], "date_from": spec.get("date_from"),
            "date_to": spec.get("date_to"), "rows": sum(len(x.get("rows") or []) for x in sections),
            "section_rows": [len(x.get("rows") or []) for x in sections],
            "queries": [x.get("query") or "" for x in sections], "spec": spec}


def _upgrade(doc: dict) -> dict | None:
    """A report saved as one table (schema 1) in the sections form."""
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    dataset = DATASETS.get(spec.get("dataset"))
    if dataset is None or not isinstance(doc.get("rows"), list) or not isinstance(doc.get("columns"), list):
        return None
    section = {"dataset": spec["dataset"], "label": dataset["label"], "mode": dataset["mode"],
               "date_from": spec.get("date_from"), "date_to": spec.get("date_to"),
               "columns": doc["columns"], "rows": doc["rows"], "total": doc.get("total", len(doc["rows"])),
               "truncated": doc.get("truncated") is True, "meta": doc.get("meta") or {}, "query": doc.get("query") or ""}
    return {**doc, "schema_version": SCHEMA, "sections": [section],
            "spec": {"nvrs": spec.get("nvrs") or [], "date_from": spec.get("date_from"),
                     "date_to": spec.get("date_to"), "sections": [{k: spec.get(k) for k in SECTION_KEYS}]}}


def list_saved(directory: Path | None = None) -> list[dict]:
    directory = directory or saved_dir()
    index = directory / "index.json"
    try:
        items = json.loads(index.read_text(encoding="utf-8"))
        if isinstance(items, list) and all(isinstance(i, dict) and SAVED_ID.fullmatch(str(i.get("id"))) for i in items):
            return items
    except (OSError, ValueError):
        pass
    return _reindex(directory)


def _reindex(directory: Path) -> list[dict]:
    items = []
    try:
        paths = list(directory.glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        if path.name == "index.json" or not SAVED_ID.fullmatch(path.stem) or path.is_symlink():
            continue
        doc = load_saved(path.stem, directory)
        if doc:
            items.append(_item(doc))
    items.sort(key=lambda i: str(i.get("created_at") or ""), reverse=True)
    try:
        if directory.is_dir():
            _write_json(directory / "index.json", items)
    except OSError:
        pass
    return items


def load_saved(report_id: str, directory: Path | None = None) -> dict | None:
    if not SAVED_ID.fullmatch(str(report_id)):
        return None
    path = (directory or saved_dir()) / f"{report_id}.json"
    try:
        if path.is_symlink():
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("id") != report_id:
        return None
    if doc.get("schema_version") == 1:
        return _upgrade(doc)
    sections = doc.get("sections")
    ok = (doc.get("schema_version") == SCHEMA and isinstance(sections, list) and sections
          and all(isinstance(x, dict) and isinstance(x.get("rows"), list) and isinstance(x.get("columns"), list)
                  for x in sections))
    return doc if ok else None


def save(name: str, spec: dict, result: dict, directory: Path | None = None) -> tuple[dict | None, str | None]:
    directory = directory or saved_dir()
    name = clean_name(name)
    if not name:
        return None, "Give the report a name."
    if sum(len(x["rows"]) for x in result["sections"]) > MAX_TOTAL_ROWS:
        return None, (f"More than {MAX_TOTAL_ROWS:,} rows in total; add conditions, a row limit "
                      "or fewer NVRs or days.")
    with _save_lock:
        items = list_saved(directory)
        if len(items) >= MAX_SAVED:
            return None, f"{MAX_SAVED} reports are saved already; delete some first."
        doc = {"schema_version": SCHEMA, "id": uuid.uuid4().hex[:12], "name": name,
               "created_at": datetime.now(timezone.utc).isoformat(), "spec": spec,
               "sections": result["sections"]}
        _write_json(directory / f"{doc['id']}.json", doc)
        item = _item(doc)
        _write_json(directory / "index.json", [item] + [i for i in items if i["id"] != item["id"]])
    return item, None


def delete_saved(report_id: str, directory: Path | None = None) -> bool:
    directory = directory or saved_dir()
    if not SAVED_ID.fullmatch(str(report_id)):
        return False
    with _save_lock:
        path = directory / f"{report_id}.json"
        try:
            if path.is_symlink() or not path.is_file():
                return False
            path.unlink()
        except OSError:
            return False
        items = [i for i in list_saved(directory) if i["id"] != report_id]
        _write_json(directory / "index.json", items)
    return True


# ---------- downloads ----------

def file_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:60]
    return stem or "report"


def _cell(value: object, kind: str) -> str:
    if value is None or value == "":
        return ""
    if kind == "percent" and isinstance(value, (int, float)):
        return f"{value:.1f}"
    return str(value)


def to_csv(section: dict) -> bytes:
    """One section as CSV: UTF-8 with BOM, so Excel shows Khmer names correctly."""
    out = io.StringIO()
    writer = csv.writer(out)
    kinds = [c.get("kind", "text") for c in section["columns"]]
    writer.writerow([c["label"] for c in section["columns"]])
    for row in section["rows"]:
        cells = [_cell(v, k) for v, k in zip(row, kinds)]
        writer.writerow([csv_safe(c) for c in cells])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def csv_download(doc: dict) -> tuple[bytes, str, str]:
    """(body, content type, extension): one CSV, or a ZIP with one CSV per section."""
    sections = doc["sections"]
    if len(sections) == 1:
        return to_csv(sections[0]), "text/csv; charset=utf-8", "csv"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for number, section in enumerate(sections, 1):
            name = f"{number:02d}-{file_stem(section.get('label') or section.get('dataset') or 'section')}.csv"
            archive.writestr(name, to_csv(section))
    return out.getvalue(), "application/zip", "zip"


def to_pdf(doc: dict) -> bytes:
    from io import BytesIO

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from nvr_track.reports.monthly_pdf import BASE, HEAD, INK, LINE, MUTED, SMALL, SUB, TITLE, clean

    cell = ParagraphStyle("rcell", parent=BASE, fontSize=7.2, leading=8.8)
    head = ParagraphStyle("rhead", parent=cell, fontName="Helvetica-Bold")
    h2 = ParagraphStyle("rh2", parent=BASE, fontName="Helvetica-Bold", fontSize=11.5, leading=14, spaceBefore=4)
    spec = doc.get("spec") or {}
    sections = doc["sections"]
    labels = " + ".join(x.get("label") or "" for x in sections)
    dates = sorted({(x.get("mode"), x.get("date_from"), x.get("date_to")) for x in sections},
                   key=lambda d: (d[0] != "range", str(d)))  # The period first, then "on <date>".
    when = "; ".join(f"on {b}" if m == "as_of" else f"{a} to {b}" for m, a, b in dates)
    nvrs = ", ".join(spec.get("nvrs") or []) or "all NVRs"
    story = [Paragraph(clean(doc.get("name"), 120), TITLE),
             Paragraph(clean(f"{labels} · {when} · {nvrs} · made {_local(doc.get('created_at')) or ''}", 300), SUB),
             Spacer(1, 4 * mm)]
    weight = {"text": 1.6, "enum": 1.3, "time": 1.5, "number": 1.1, "percent": 1.1}
    wide = {"camera", "findings", "detail", "text", "description", "known_issue"}
    width = landscape(A4)[0] - 24 * mm
    for number, section in enumerate(sections, 1):
        if number > 1:
            story += [Spacer(1, 6 * mm), CondPageBreak(55 * mm)]
        shown = section["rows"][:PDF_ROWS]
        when = (f"on {section.get('date_to')}" if section.get("mode") == "as_of"
                else f"{section.get('date_from')} to {section.get('date_to')}")
        title = f"{number}. {section.get('label')}" if len(sections) > 1 else str(section.get("label"))
        total = section.get("total", len(section["rows"]))
        story += [Paragraph(clean(f"{title} · {when} · {total} {'row' if total == 1 else 'rows'}", 200), h2),
                  Paragraph(clean(section.get("query"), 900), SMALL),
                  Paragraph(clean("Source: " + str((section.get("meta") or {}).get("source") or ""), 300), SMALL)]
        if len(section["rows"]) > len(shown) or section.get("truncated"):
            story.append(Paragraph(clean(f"Showing {len(shown)} of {section.get('total')} rows; the CSV download "
                                         f"has {len(section['rows'])}.", 200), SMALL))
        story.append(Spacer(1, 2.5 * mm))
        kinds = [c.get("kind", "text") for c in section["columns"]]
        weights = [1.0 if c["key"] == "nvr" else weight.get(k, 1.4) * (1.6 if c["key"] in wide else 1)
                   for k, c in zip(kinds, section["columns"])]
        widths = [width * w / sum(weights) for w in weights]
        data = [[Paragraph(clean(c["label"], 60), head) for c in section["columns"]]]
        data += [[Paragraph(clean(_cell(v, k), 200), cell) for v, k in zip(row, kinds)] for row in shown]
        if not shown:
            data.append([Paragraph("No rows match this query.", cell)] + [""] * (len(widths) - 1))
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TEXTCOLOR", (0, 0), (-1, -1), INK)]))
        story.append(table)

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(12 * mm, 7 * mm, "NVR Track · built from saved reports; no NVR was contacted")
        canvas.drawRightString(landscape(A4)[0] - 12 * mm, 7 * mm, f"Page {document.page}")
        canvas.restoreState()
    out = BytesIO()
    SimpleDocTemplate(out, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                      bottomMargin=13 * mm, title=clean(doc.get("name"), 120), author="NVR Track").build(
        story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
