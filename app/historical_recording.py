"""Bounded historical recording searches. No camera or NVR settings are changed.

Historical evidence is independent of the recent recording status. Searches are
sequential, limited to 24-hour intervals, and stop once evidence is confirmed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import time
from typing import Callable

from app.recording import check_recording_evidence

FOUND = "HISTORICAL_RECORDING_FOUND"
ABSENT = "NO_HISTORICAL_EVIDENCE_IN_WINDOW"
INCONCLUSIVE = "HISTORICAL_CHECK_INCONCLUSIVE"
ERROR = "HISTORICAL_CHECK_ERROR"
NOT_CHECKED = "HISTORICAL_NOT_CHECKED"
NOT_CONFIGURED = "HISTORICAL_NOT_CONFIGURED"


def empty_result(status: str, message: str, track_id: int | None = None) -> dict:
    return {
        "status": status, "message": message, "track_id": track_id,
        "checked_at": None, "most_recent_segment_start": None,
        "most_recent_segment_end": None, "num_matches": None,
        "requested_window_utc": {"start": None, "end": None},
        "intervals_examined": [], "search_complete": False,
        "from_cache": False,
    }


def intervals_for_history(nvr_now: datetime, recent_minutes: int,
                          lookback_days: int) -> list[tuple[datetime, datetime]]:
    """Return newest-first, non-overlapping intervals covering only older time."""
    if nvr_now.tzinfo is None or not 1 <= recent_minutes <= 1440 or not 1 <= lookback_days <= 7:
        raise ValueError("Invalid timezone or historical search limits")
    end = nvr_now.astimezone(timezone.utc).replace(microsecond=0) - timedelta(minutes=recent_minutes)
    oldest = nvr_now.astimezone(timezone.utc).replace(microsecond=0) - timedelta(days=lookback_days)
    intervals = []
    while end > oldest:
        start = max(oldest, end - timedelta(days=1))
        intervals.append((start, end))
        end = start
    return intervals


def valid_cache(previous: object, track_id: int, *, now: datetime,
                ttl_hours: int, oldest: datetime) -> dict | None:
    """Reuse only successful, recent checks for the same verified track."""
    if not isinstance(previous, dict) or previous.get("track_id") != track_id:
        return None
    if previous.get("status") not in {FOUND, ABSENT}:
        return None
    try:
        checked = datetime.fromisoformat(previous["checked_at"])
        if checked.tzinfo is None or not timedelta(0) <= now - checked <= timedelta(hours=ttl_hours):
            return None
        if previous["status"] == FOUND:
            end = datetime.fromisoformat(previous["most_recent_segment_end"])
            if end.tzinfo is None or end < oldest:
                return None
        elif not previous.get("search_complete"):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    cached = {**previous, "from_cache": True}
    return cached


def search_history(*, ip: str, port: int, username: str, password: str,
                   track_id: int, clock_snapshot: tuple[datetime, tzinfo],
                   recent_minutes: int, lookback_days: int, max_results: int,
                   deadline: float, search: Callable = check_recording_evidence) -> dict:
    now, tz = clock_snapshot
    windows = intervals_for_history(now, recent_minutes, lookback_days)
    result = empty_result(INCONCLUSIVE, "Historical search not completed.", track_id)
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    result["num_matches"] = 0
    result["requested_window_utc"] = {
        "start": windows[-1][0].isoformat(), "end": windows[0][1].isoformat()
    }
    for start, end in windows:
        if time.monotonic() >= deadline:
            result["message"] = "Historical request budget reached; remaining intervals not checked."
            return result
        try:
            raw = search(
                ip=ip, port=port, username=username, password=password,
                track_id=track_id, minutes=recent_minutes, max_results=max_results,
                clock_snapshot=(now, tz),
                window_start_utc=start, window_end_utc=end,
            )
        except Exception:
            result["status"] = ERROR
            result["message"] = "Historical search failed unexpectedly."
            return result
        status = raw.get("status")
        result["intervals_examined"].append({
            "start": start.isoformat(), "end": end.isoformat(), "status": str(status)[:80],
        })
        if status in {"RECORDING_EVIDENCE_FOUND", "RECORDING_EVIDENCE_FOUND_WITH_TIME_ANOMALY"}:
            matches = []
            for item in (raw.get("matches") if isinstance(raw.get("matches"), list) else []):
                if not isinstance(item, dict) or item.get("track_id") != track_id:
                    continue
                try:
                    s = datetime.fromisoformat(item["start_utc"])
                    e = datetime.fromisoformat(item["end_utc"])
                    if s.tzinfo and e.tzinfo and s < e and s <= end and e >= start:
                        matches.append((s, e))
                except (TypeError, ValueError, KeyError):
                    pass
            if not matches:
                result["message"] = "Historical evidence could not be verified for this track and interval."
                return result
            newest = max(matches, key=lambda match: match[1])
            result.update({
                "status": FOUND,
                "message": "Verified stored recording overlaps a historical interval."
                           + (" Time anomalies require review." if raw.get("anomalies") else ""),
                "num_matches": len(matches),
                "most_recent_segment_start": newest[0].isoformat(),
                "most_recent_segment_end": newest[1].isoformat(),
                # Finding evidence proves existence, but not full coverage.
                "search_complete": False,
            })
            return result
        if status != "NO_MATCHES_IN_WINDOW":
            result["status"] = ERROR if status in {
                "AUTHENTICATION_FAILED", "PERMISSION_DENIED", "ENDPOINT_UNAVAILABLE",
                "SEARCH_REQUEST_FAILED", "CONNECTION_OR_REQUEST_ERROR",
                "DEVICE_REPORTED_SEARCH_ERROR", "INVALID_CONFIGURATION",
            } else INCONCLUSIVE
            result["message"] = "Historical interval was not reliably searchable: " + str(status)[:80]
            return result
    result["status"] = ABSENT
    result["message"] = "No evidence in all examined historical intervals; not proof footage never existed."
    result["search_complete"] = True
    return result


def summarize_history(channels: list[dict], enabled: bool) -> dict:
    statuses = [ch.get("historical_recording_result", {}).get("status") for ch in channels]
    return {
        "enabled": enabled,
        "evidence": statuses.count(FOUND),
        "no_evidence": statuses.count(ABSENT),
        "inconclusive": statuses.count(INCONCLUSIVE),
        "errors": statuses.count(ERROR),
        "not_checked": statuses.count(NOT_CHECKED) + statuses.count(NOT_CONFIGURED),
    }
