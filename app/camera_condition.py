"""One plain verdict per camera, built only from results already in a report.

Used by the dashboard and the daily email so both say the same thing.
Nothing here contacts an NVR.
"""
from __future__ import annotations

EVIDENCE = {"RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY"}
FAILED = {"TRACK_NOT_FOUND", "TRACK_MAPPING_UNVERIFIED", "CHECK_INCONCLUSIVE",
          "CHECK_ERROR", "NOT_CHECKED", "NOT_CONFIGURED"}
CALENDAR_KNOWN = {"RETENTION_VERIFIED", "RETENTION_PARTIAL", "RETENTION_STALE"}

# code: (label, severity, rank) - rank orders the most urgent first.
CONDITIONS = {
    "NO_FOOTAGE_30": ("No footage 30+ days", "critical", 0),
    "NOT_RECORDING": ("Stopped recording", "critical", 1),
    "CHECK_FAILED": ("Check failed", "warning", 2),
    "GAPS": ("Gaps", "warning", 3),
    "OFFLINE_RECORDING": ("Offline, still recording", "warning", 4),
    "SHORT_HISTORY": ("Short history", "info", 5),
    "OK": ("OK", "ok", 6),
}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


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

    if status in EVIDENCE:
        if live == "OFFLINE":
            code, detail = "OFFLINE_RECORDING", "Reported offline, but video was recorded in the last hour."
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
        code, detail = "CHECK_FAILED", "Per-camera recording checks are not enabled for this NVR."
    elif status in FAILED or status is None:
        code, detail = "CHECK_FAILED", "The recording check could not be completed."
    elif history.get("status") == "HISTORICAL_RECORDING_FOUND":
        code, detail = "NOT_RECORDING", "No video in the last hour; older video found in the last 7 days."
    elif cal["known"] and cal["with_footage"] == 0:
        code = "NO_FOOTAGE_30"
        detail = f"No video on any of the last {cal['window']} days."
    else:
        code = "NOT_RECORDING"
        detail = ("No video in the last hour; last recorded day " + cal["last_day"] + "."
                  if cal["last_day"] else "No video in the last hour.")

    label, severity, rank = CONDITIONS[code]
    return {"code": code, "label": label, "severity": severity, "rank": rank,
            "detail": detail, "missing_days": cal["missing_days"],
            "history_days": cal["history_days"], "last_day": cal["last_day"]}


def count(channels: list[dict]) -> dict[str, int]:
    totals = {code: 0 for code in CONDITIONS}
    for channel in channels:
        totals[classify(channel)["code"]] += 1
    return totals
