"""Known issues and the repair log, shared by the dashboard, Telegram and email.

One small JSON file (state/notes/notes.json) written by the dashboard and the
Telegram bot under a file lock, read by everyone. Nothing here contacts an
NVR. A known issue pauses alerts for a camera (or a whole NVR) until a date or
until someone marks it fixed; the repair log keeps dated notes per camera.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
CHANNEL_ID = re.compile(r"[0-9]{1,4}")
MAX_TEXT = 300
MAX_DAYS = 180
MAX_LOG = 2000            # Newest entries kept in the repair log.
MAX_ISSUES = 500
# Conditions a known issue replaces (problems someone is already handling).
PROBLEM_CODES = {"NO_FOOTAGE_30", "NOT_RECORDING", "CHECK_FAILED", "GAPS",
                 "BELOW_RETENTION", "BAD_PICTURE", "OFFLINE_RECORDING"}


def notes_dir() -> Path:
    return Path(os.environ.get("NOTES_DIR", str(ROOT / "state" / "notes")))


def today_local() -> date:
    return datetime.now(LOCAL_TZ).date()


def clean_text(text: object) -> str:
    value = "".join(ch for ch in str(text or "") if ch == " " or ch.isprintable())
    return " ".join(value.split())[:MAX_TEXT]


def valid_target(nvr_id: object, channel: object) -> bool:
    return (isinstance(nvr_id, str) and bool(DEVICE_ID.fullmatch(nvr_id))
            and (channel is None or (isinstance(channel, str) and bool(CHANNEL_ID.fullmatch(channel)))))


def parse_until(text: str, today: date | None = None) -> date | None | str:
    """'7d', '2w', '2026-11-10' -> date; '-', 'open', '' -> None; else 'invalid'."""
    today = today or today_local()
    value = (text or "").strip().lower()
    if value in {"", "-", "open", "none"}:
        return None
    match = re.fullmatch(r"(\d{1,3})\s*([dw])", value)
    if match:
        days = int(match[1]) * (7 if match[2] == "w" else 1)
        return today + timedelta(days=days) if 1 <= days <= MAX_DAYS else "invalid"
    try:
        until = date.fromisoformat(value)
    except ValueError:
        return "invalid"
    return until if today <= until <= today + timedelta(days=MAX_DAYS) else "invalid"


# ---------- file access ----------

def _empty() -> dict:
    return {"schema_version": 1, "known_issues": [], "log": []}


def load(directory: Path | None = None) -> dict:
    path = (directory or notes_dir()) / "notes.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return _empty()
    issues = [i for i in data.get("known_issues", []) if isinstance(i, dict)
              and valid_target(i.get("nvr"), i.get("channel"))]
    log = [n for n in data.get("log", []) if isinstance(n, dict)
           and valid_target(n.get("nvr"), n.get("channel"))]
    return {"schema_version": 1, "known_issues": issues, "log": log}


@contextmanager
def _locked(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / ".notes.lock", "a+")
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # Windows development copy.
            pass
        yield
    finally:
        handle.close()


def _update(change, directory: Path | None = None):
    directory = directory or notes_dir()
    with _locked(directory):
        data = load(directory)
        result = change(data)
        data["log"] = data["log"][-MAX_LOG:]
        data["known_issues"] = data["known_issues"][-MAX_ISSUES:]
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".notes", suffix=".tmp", delete=False) as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            temporary = Path(handle.name)
        os.replace(temporary, directory / "notes.json")
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_entry(nvr_id: str, channel: str | None, text: str, by: str, kind: str = "note") -> dict:
    return {"id": uuid.uuid4().hex[:12], "nvr": nvr_id, "channel": channel, "kind": kind,
            "text": clean_text(text), "by": clean_text(by)[:60], "at": _now()}


def add_known_issue(nvr_id: str, channel: str | None, reason: str, until: date | None,
                    by: str, directory: Path | None = None) -> dict:
    """Add (or replace) the known issue for this camera / whole NVR."""
    if not valid_target(nvr_id, channel):
        raise ValueError("Invalid NVR or channel")
    reason = clean_text(reason) or "Known issue"
    item = {"id": uuid.uuid4().hex[:12], "nvr": nvr_id, "channel": channel, "reason": reason,
            "until": until.isoformat() if until else None, "created_at": _now(),
            "by": clean_text(by)[:60]}

    def change(data):
        data["known_issues"] = [i for i in data["known_issues"]
                                if not (i["nvr"] == nvr_id and i.get("channel") == channel)]
        data["known_issues"].append(item)
        end = f" until {item['until']}" if item["until"] else " until marked fixed"
        data["log"].append(_log_entry(nvr_id, channel, f"Known issue{end}: {reason}", by, "known"))
        return item
    return _update(change, directory)


def clear_known_issue(nvr_id: str, channel: str | None, by: str, text: str = "",
                      directory: Path | None = None) -> bool:
    if not valid_target(nvr_id, channel):
        raise ValueError("Invalid NVR or channel")

    def change(data):
        before = len(data["known_issues"])
        data["known_issues"] = [i for i in data["known_issues"]
                                if not (i["nvr"] == nvr_id and i.get("channel") == channel)]
        removed = len(data["known_issues"]) < before
        if removed or text:
            data["log"].append(_log_entry(nvr_id, channel, "Fixed" + (f": {text}" if text else ""), by, "fixed"))
        return removed
    return _update(change, directory)


def add_note(nvr_id: str, channel: str | None, text: str, by: str,
             directory: Path | None = None) -> dict:
    if not valid_target(nvr_id, channel) or not clean_text(text):
        raise ValueError("Invalid NVR, channel or empty note")
    return _update(lambda data: data["log"].append(_log_entry(nvr_id, channel, text, by)) or data["log"][-1],
                   directory)


# ---------- reading ----------

def active_issues(data: dict, today: date | None = None) -> list[dict]:
    today = today or today_local()
    active = []
    for item in data.get("known_issues", []):
        try:
            if item.get("until") is None or date.fromisoformat(item["until"]) >= today:
                active.append(item)
        except (TypeError, ValueError):
            continue
    return active


def stale_days(env: dict | None = None) -> int:
    """NVR_KNOWN_REMINDER_DAYS: a known issue open this long is flagged for review (default 7)."""
    env = os.environ if env is None else env
    try:
        days = int(str(env.get("NVR_KNOWN_REMINDER_DAYS", "7")).strip())
    except ValueError:
        return 7
    return days if 1 <= days <= MAX_DAYS else 7


def opened_on(item: dict | None) -> date | None:
    """Local date the known issue was set; None when the stamp is missing or invalid."""
    try:
        moment = datetime.fromisoformat(str((item or {}).get("created_at") or ""))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).date()


def age_days(item: dict | None, today: date | None = None) -> int | None:
    """Whole days the known issue has been open (0 = set today); None when unknown."""
    start = opened_on(item)
    return None if start is None else max(((today or today_local()) - start).days, 0)


def issue_for(issues: list[dict], nvr_id: str, channel: str | None) -> dict | None:
    """The camera's own known issue, else one for its whole NVR."""
    own = next((i for i in issues if i["nvr"] == nvr_id and channel is not None
                and i.get("channel") == channel), None)
    return own or next((i for i in issues if i["nvr"] == nvr_id and i.get("channel") is None), None)


def notes_for(data: dict, nvr_id: str, channel: str | None, limit: int = 5) -> list[dict]:
    rows = [n for n in data.get("log", []) if n["nvr"] == nvr_id
            and (channel is None or n.get("channel") in {channel, None})]
    return rows[-limit:][::-1]


def public_issue(item: dict | None) -> dict | None:
    if not item:
        return None
    return {"reason": clean_text(item.get("reason")), "until": item.get("until"),
            "whole_nvr": item.get("channel") is None, "by": clean_text(item.get("by"))[:60],
            "created_at": item.get("created_at")}


def public_note(item: dict) -> dict:
    return {"text": clean_text(item.get("text")), "by": clean_text(item.get("by"))[:60],
            "at": item.get("at"), "kind": item.get("kind", "note"), "channel": item.get("channel")}


def overlay(nvr_id: str, channels: list[dict], data: dict, known_condition: dict,
            today: date | None = None) -> None:
    """Attach known issues and recent notes to channel rows (in place).

    A camera with a known issue and a problem condition shows the
    KNOWN_ISSUE condition instead; the original stays in condition['original'].
    """
    issues = active_issues(data, today)
    for ch in channels:
        cid = str(ch.get("channel_id", ""))
        issue = issue_for(issues, nvr_id, cid)
        ch["known_issue"] = public_issue(issue)
        ch["notes"] = [public_note(n) for n in notes_for(data, nvr_id, cid, 3)]
        condition = ch.get("condition") or {}
        if issue and condition.get("code") in PROBLEM_CODES:
            until = f" until {issue['until']}" if issue.get("until") else ""
            ch["condition"] = {**known_condition, "original": condition.get("code"),
                               "detail": f"Known issue{until}: {clean_text(issue.get('reason'))} "
                                         f"(was: {condition.get('label', '')})",
                               "missing_days": condition.get("missing_days"),
                               "history_days": condition.get("history_days"),
                               "last_day": condition.get("last_day")}
