"""One plain verdict per camera, built only from results already in a report.

Used by the dashboard and the daily email so both say the same thing.
Nothing here contacts an NVR.
"""
from __future__ import annotations

from datetime import datetime

EVIDENCE = {"RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY"}
FAILED = {"TRACK_NOT_FOUND", "TRACK_MAPPING_UNVERIFIED", "CHECK_INCONCLUSIVE",
          "CHECK_ERROR", "NOT_CHECKED"}
CALENDAR_KNOWN = {"RETENTION_VERIFIED", "RETENTION_PARTIAL", "RETENTION_STALE"}

# code: (label, severity, rank) - rank orders the most urgent first.
CONDITIONS = {
    "NO_FOOTAGE_30": ("No footage 30+ days", "critical", 0),
    "NOT_RECORDING": ("Stopped recording", "critical", 1),
    "CHECK_FAILED": ("Check failed", "warning", 2),
    "GAPS": ("Gaps", "warning", 3),
    "OFFLINE_RECORDING": ("Offline, still recording", "warning", 4),
    "SHORT_HISTORY": ("Short history", "info", 5),
    # Online camera marked "event" in recording_expectations: a quiet window
    # is expected, so it is not "Stopped recording" (README, reporting.py).
    "NO_RECENT_EVENTS": ("No recent events", "info", 6),
    # Checks are switched off for this NVR: unknown, never counted as OK.
    "NOT_MONITORED": ("Not monitored", "info", 7),
    "OK": ("OK", "ok", 8),
}


def window_text(recording: dict) -> str:
    """'the last 60 minutes' from the report's own search window."""
    minutes = recording.get("window_minutes")
    if type(minutes) is not int or minutes <= 0:
        return "the recent search window"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "the last hour" if hours == 1 else f"the last {hours} hours"
    return f"the last {minutes} minutes"


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def history_text(history: dict, recording: dict) -> str:
    """'the last 7 days' from the historical search's own window
    (NVR_HISTORICAL_LOOKBACK_DAYS), not a fixed number."""
    window = _dict(history.get("requested_window_utc"))
    try:
        span = (datetime.fromisoformat(window["end"])
                - datetime.fromisoformat(window["start"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return "the last few days"
    minutes = recording.get("window_minutes")
    if type(minutes) is int and minutes > 0:
        span += minutes * 60  # The history window ends where the recent one starts.
    days = round(span / 86400)
    if days < 1:
        return "the last few days"
    return "the last day" if days == 1 else f"the last {days} days"


def calendar_facts(retention: dict) -> dict:
    """Recorded-day facts from the 30-day calendar; None where unknown."""
    retention = _dict(retention)
    known = (retention.get("status") in CALENDAR_KNOWN
             and type(retention.get("days_with_footage")) is int)
    days = sorted((row for row in retention.get("calendar") or [] if isinstance(row, dict)
                   and isinstance(row.get("date"), str)), key=lambda row: row["date"])
    window = retention.get("window_days") if type(retention.get("window_days")) is int else len(days)
    present = [i for i, row in enumerate(days) if row.get("has_recording") is True]
    first = present[0] if present else None
    missing = (sum(row.get("has_recording") is False for row in days[first:])
               if first is not None else 0)
    return {
        "known": known,
        "window": window,
        "with_footage": retention.get("days_with_footage") if known else None,
        "history_days": (len(days) - first) if first is not None else 0,
        "missing_days": missing,
        "last_day": days[present[-1]]["date"] if present else None,
    }


def classify(channel: dict) -> dict:
    """Return code, label, severity, rank and a short reason for one camera."""
    recording = _dict(channel.get("recording_result"))
    history = _dict(channel.get("historical_recording_result"))
    status = recording.get("status")
    live = channel.get("reported_status")
    cal = calendar_facts(channel.get("retention_result"))
    window = window_text(recording)

    if status in EVIDENCE:
        if live == "OFFLINE":
            code, detail = "OFFLINE_RECORDING", f"Reported offline, but video was recorded in {window}."
        elif cal["known"] and cal["missing_days"]:
            code = "GAPS"
            detail = f"{cal['missing_days']} of the last {cal['history_days']} days have no video."
        elif cal["known"] and cal["history_days"] < cal["window"]:
            code = "SHORT_HISTORY"
            detail = (f"Footage kept for only {cal['history_days']} of {cal['window']} days "
                      "(overwritten, or camera added recently).")
        else:
            code, detail = "OK", "Recording now."
    elif status == "NOT_CONFIGURED":
        code, detail = "NOT_MONITORED", "Per-camera recording checks are not enabled for this NVR."
    elif status in FAILED or status is None:
        code, detail = "CHECK_FAILED", "The recording check could not be completed."
    elif (channel.get("recording_expectation") == "event" and live == "ONLINE"
          and not (cal["known"] and cal["with_footage"] == 0)):
        # Event/motion recording: no segment in a quiet window is normal.
        # Offline event cameras and 30 empty calendar days stay problems.
        code = "NO_RECENT_EVENTS"
        detail = f"Event recording: no events in {window}."
        if history.get("status") == "HISTORICAL_RECORDING_FOUND":
            detail += f" Older video found in {history_text(history, recording)}."
        elif cal["last_day"]:
            detail += f" Last recorded day {cal['last_day']}."
    elif history.get("status") == "HISTORICAL_RECORDING_FOUND":
        code = "NOT_RECORDING"
        detail = f"No video in {window}; older video found in {history_text(history, recording)}."
    elif cal["known"] and cal["with_footage"] == 0:
        code = "NO_FOOTAGE_30"
        detail = f"No video on any of the last {cal['window']} days."
    else:
        code = "NOT_RECORDING"
        detail = (f"No video in {window}; last recorded day " + cal["last_day"] + "."
                  if cal["last_day"] else f"No video in {window}.")

    label, severity, rank = CONDITIONS[code]
    return {"code": code, "label": label, "severity": severity, "rank": rank,
            "detail": detail, "missing_days": cal["missing_days"],
            "history_days": cal["history_days"], "last_day": cal["last_day"]}


def count(channels: list[dict]) -> dict[str, int]:
    totals = {code: 0 for code in CONDITIONS}
    for channel in channels:
        totals[classify(channel)["code"]] += 1
    return totals
