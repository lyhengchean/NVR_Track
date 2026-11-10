"""One plain verdict per camera, built only from results already in a report.

Used by the dashboard and the daily email so both say the same thing.
Nothing here contacts an NVR.
"""
from __future__ import annotations

import os
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
    # Keeps fewer days of footage than NVR_REQUIRED_RETENTION_DAYS.
    "BELOW_RETENTION": ("Below required days", "warning", 4),
    # Recording, but the snapshot is black, blank/covered, frozen or moved.
    "BAD_PICTURE": ("Picture problem", "warning", 5),
    "OFFLINE_RECORDING": ("Offline, still recording", "warning", 6),
    "SHORT_HISTORY": ("Short history", "info", 7),
    # Set by nvr_track.monitoring.notes.overlay: someone marked the problem as being handled.
    "KNOWN_ISSUE": ("Known issue", "info", 8),
    # Online camera marked "event" in recording_expectations: a quiet window
    # is expected, so it is not "Stopped recording" (README, reporting.py).
    "NO_RECENT_EVENTS": ("No recent events", "info", 9),
    # Checks are switched off for this NVR: unknown, never counted as OK.
    "NOT_MONITORED": ("Not monitored", "info", 10),
    "OK": ("OK", "ok", 11),
}
BAD_PICTURES = {
    "PICTURE_BLACK": "The picture is black (lens covered, IR failure or no light).",
    "PICTURE_UNIFORM": "The picture is a flat colour (covered, blurred or no signal).",
    "PICTURE_FROZEN": "The picture has not changed since the previous scan (frozen).",
    "PICTURE_VIEW_CHANGED": "The view differs from the reference picture (camera moved?).",
}


def known_condition() -> dict:
    label, severity, rank = CONDITIONS["KNOWN_ISSUE"]
    return {"code": "KNOWN_ISSUE", "label": label, "severity": severity, "rank": rank}


def required_retention_days() -> int:
    """NVR_REQUIRED_RETENTION_DAYS (1-90); 0 or invalid turns the rule off."""
    try:
        days = int(os.environ.get("NVR_REQUIRED_RETENTION_DAYS", "0"))
    except ValueError:
        return 0
    return days if 1 <= days <= 90 else 0


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


def retention_shortfall(channel: dict) -> dict | None:
    """Does the camera keep fewer days than NVR_REQUIRED_RETENTION_DAYS?

    Judged on its own, with the same rule as classify(), because there a gap,
    a picture problem or "offline, still recording" takes precedence and hides
    it. An alert about kept days must not be "resolved" by that. None: the
    rule is off, or it cannot be known now (no recent footage, no calendar).
    """
    required = required_retention_days()
    if not required or _dict(channel.get("recording_result")).get("status") not in EVIDENCE:
        return None
    cal = calendar_facts(channel.get("retention_result"))
    if not cal["known"]:
        return None
    return {"below": cal["history_days"] < cal["window"] and cal["history_days"] < required,
            "history_days": cal["history_days"], "required": required}


def classify(channel: dict) -> dict:
    """Return code, label, severity, rank and a short reason for one camera."""
    recording = _dict(channel.get("recording_result"))
    history = _dict(channel.get("historical_recording_result"))
    status = recording.get("status")
    live = channel.get("reported_status")
    cal = calendar_facts(channel.get("retention_result"))
    window = window_text(recording)

    picture = _dict(channel.get("snapshot_result")).get("status")
    if status in EVIDENCE:
        if live == "OFFLINE":
            code, detail = "OFFLINE_RECORDING", f"Reported offline, but video was recorded in {window}."
        elif picture in BAD_PICTURES:
            code, detail = "BAD_PICTURE", "Recording, but " + BAD_PICTURES[picture][0].lower() + BAD_PICTURES[picture][1:]
        elif cal["known"] and cal["missing_days"]:
            code = "GAPS"
            detail = f"{cal['missing_days']} of the last {cal['history_days']} days have no video."
        elif cal["known"] and cal["history_days"] < cal["window"]:
            required = required_retention_days()
            if required and cal["history_days"] < required:
                code = "BELOW_RETENTION"
                detail = (f"Footage kept for only {cal['history_days']} days; {required} days "
                          "are required (disk too small, or camera added recently).")
            else:
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


def kept_days(channels: list[dict]) -> dict:
    """How many days of footage an NVR keeps, from its cameras' 30-day calendars.

    Only cameras recording now with a fully verified calendar count: a camera
    that stopped, or whose calendar is partial or old, says nothing about how
    long the disks keep video. Per camera: days from its oldest recorded day
    to yesterday (as in "Footage kept for only N days"); the window itself
    means "at least the window", because older days are not read.
    typical is the median camera; fewest may be a camera added recently.
    """
    found = []
    for channel in channels:
        channel = _dict(channel)
        retention = _dict(channel.get("retention_result"))
        if (_dict(channel.get("recording_result")).get("status") not in EVIDENCE
                or retention.get("status") != "RETENTION_VERIFIED"):
            continue
        cal = calendar_facts(retention)
        if cal["known"] and cal["history_days"] > 0 and type(cal["window"]) is int and cal["window"] > 0:
            found.append((min(cal["history_days"], cal["window"]), cal["window"], str(channel.get("channel_id", ""))))
    if not found:
        return {"cameras": 0, "window": None, "typical": None, "fewest": None, "fewest_channels": [],
                "fewest_count": 0, "full": 0}
    values = sorted(days for days, _, _ in found)
    fewest = values[0]
    channels_fewest = sorted((cid for days, _, cid in found if days == fewest),
                             key=lambda cid: int(cid) if cid.isdigit() else 10**6)
    return {"cameras": len(found), "window": max(window for _, window, _ in found),
            "typical": values[(len(values) - 1) // 2], "fewest": fewest,
            "fewest_channels": channels_fewest[:5], "fewest_count": len(channels_fewest),
            "full": sum(days >= window for days, window, _ in found)}


def kept_text(kept: dict | None) -> str:
    """'21 days (fewest 9: ch12)', '30+ days' or 'unknown'."""
    kept = _dict(kept)
    if not kept.get("cameras") or kept.get("typical") is None:
        return "unknown (no verified calendar)"

    def days(n):
        return f"{n}+ days" if n >= kept["window"] else ("1 day" if n == 1 else f"{n} days")
    text = days(kept["typical"])
    if kept["fewest"] < kept["typical"]:
        more = (kept.get("fewest_count") or 0) - len(kept["fewest_channels"])
        text += (f" (fewest {kept['fewest']}: " + ", ".join(f"ch{c}" for c in kept["fewest_channels"])
                 + (f" +{more} more" if more > 0 else "") + ")")
    return text
