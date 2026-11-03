"""Bounded, read-only recording calendar queries for verified NVR camera tracks.

DailyDistribution reports dates with *some* recording, not continuous coverage or
an exact retention boundary. Missing/malformed dates are unknown, not 'no video'.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from ipaddress import ip_address
import time
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException

from app.recording import local_name

VERIFIED = "RETENTION_VERIFIED"
PARTIAL = "RETENTION_PARTIAL"
PENDING = "RETENTION_PENDING"
STALE = "RETENTION_STALE"
UNSUPPORTED = "RETENTION_UNSUPPORTED"
INCONCLUSIVE = "RETENTION_INCONCLUSIVE"
ERROR = "RETENTION_ERROR"
UNVERIFIED = "RETENTION_UNVERIFIED"
NOT_CONFIGURED = "RETENTION_NOT_CONFIGURED"


def empty_result(status: str, message: str, track_id: int | None = None) -> dict:
    return {
        "status": status, "message": message, "track_id": track_id,
        "checked_at": None, "as_of_date": None, "window_days": None,
        "oldest_verified_date": None, "oldest_verified_age_days": None,
        "days_with_footage": None, "days_examined": None,
        "calendar": [], "from_cache": False, "last_attempted_at": None,
    }


def _date_window(now_local: datetime, days: int) -> list[date]:
    if now_local.tzinfo is None or not 1 <= days <= 90:
        raise ValueError("Expected verified timezone and 1-90 days")
    # Exclude current incomplete local day.
    today = now_local.date()
    return [today - timedelta(days=offset) for offset in range(days, 0, -1)]


def parse_month(content: bytes, year: int, month: int) -> dict[int, bool]:
    """Accept explicit true/false day entries only; unknown dates remain unknown."""
    if len(content) > 128_000 or not content:
        raise ValueError("Empty or oversized calendar response")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Invalid calendar XML") from exc
    if local_name(root.tag).lower() != "trackdailydistribution":
        raise ValueError("Unexpected calendar XML root")
    result: dict[int, bool] = {}
    for node in root.iter():
        if local_name(node.tag).lower() != "day":
            continue
        children = {local_name(child.tag).lower(): (child.text or "").strip().lower()
                    for child in node}
        try:
            number = int(children["dayofmonth"])
        except (KeyError, ValueError) as exc:
            raise ValueError("Invalid day identifier") from exc
        if not 1 <= number <= calendar.monthrange(year, month)[1] or number in result:
            raise ValueError("Duplicate or invalid day")
        value = children.get("record")
        if value not in {"true", "false", "1", "0"}:
            raise ValueError("Invalid recorded-day indicator")
        result[number] = value in {"true", "1"}
    if not result:
        raise ValueError("No explicit calendar day records")
    return result


def query_month(*, ip: str, port: int, username: str, password: str,
                track_id: int, year: int, month: int) -> dict:
    """Single non-mutating POST; no redirects and no retries of errors."""
    ip_address(ip)
    if type(track_id) is not int or not 1 <= track_id <= 999999:
        raise ValueError("Track must have been verified")
    if not 2000 <= year <= 2100 or not 1 <= month <= 12:
        raise ValueError("Invalid calendar month")
    url = f"http://{ip}:{port}/ISAPI/ContentMgmt/record/tracks/{track_id}/dailyDistribution"
    body = (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<trackDailyParam><year>{year}</year><monthOfYear>{month}</monthOfYear>'
            f'</trackDailyParam>').encode("utf-8")
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(
                url, data=body, auth=HTTPDigestAuth(username, password),
                headers={"Accept": "application/xml", "Content-Type": "application/xml"},
                timeout=(3, 8), allow_redirects=False,
            )
    except RequestException:
        return {"status": ERROR, "message": "Calendar query connection failed or timed out."}
    if response.status_code in {404, 405, 501}:
        return {"status": UNSUPPORTED, "message": "Device does not expose the calendar endpoint."}
    if response.status_code != 200:
        return {"status": ERROR, "message": "Calendar query did not succeed."}
    try:
        return {"status": VERIFIED, "days": parse_month(response.content, year, month)}
    except ValueError:
        return {"status": INCONCLUSIVE, "message": "Calendar response could not be verified."}


def scan_calendar(*, ip: str, port: int, username: str, password: str,
                  track_id: int, clock_snapshot: tuple[datetime, object],
                  days: int = 30, deadline: float | None = None,
                  query=query_month) -> dict:
    """Query at most four months (90 days), sequentially, with deadline checks."""
    now_utc, device_tz = clock_snapshot
    if now_utc.tzinfo is None or device_tz is None:
        return empty_result(UNVERIFIED, "Device clock is not verified.", track_id)
    local_now = now_utc.astimezone(device_tz)
    dates = _date_window(local_now, days)
    result = empty_result(PENDING, "Retention scan not started.", track_id)
    result.update({"as_of_date": local_now.date().isoformat(), "window_days": days,
                   "checked_at": datetime.now(timezone.utc).isoformat(),
                   "last_attempted_at": datetime.now(timezone.utc).isoformat()})
    months = list(dict.fromkeys((day.year, day.month) for day in dates))
    outcomes: dict[tuple[int, int], dict] = {}
    for year, month in months:
        if deadline is not None and time.monotonic() >= deadline:
            return dict(result, status=PENDING, message="Retention scan deferred by the NVR time budget.",
                        checked_at=None)
        try:
            response = query(ip=ip, port=port, username=username, password=password,
                             track_id=track_id, year=year, month=month)
        except Exception:
            return dict(result, status=ERROR, message="Unexpected calendar query failure.")
        if response.get("status") != VERIFIED:
            return dict(result, status=response.get("status", INCONCLUSIVE),
                        message=str(response.get("message") or "Calendar query failed.")[:160])
        outcomes[(year, month)] = response["days"]
    calendar_days = [{"date": day.isoformat(), "has_recording":
                      outcomes[(day.year, day.month)].get(day.day)} for day in dates]
    found = [date.fromisoformat(row["date"]) for row in calendar_days
             if row["has_recording"] is True]
    known = sum(row["has_recording"] is not None for row in calendar_days)
    oldest = min(found) if found else None
    complete = known == days
    result.update({
        "status": VERIFIED if complete else PARTIAL,
        "message": ("Daily recording calendar verified for the selected window."
                    if complete else "Some dates were omitted by the NVR; missing dates are unknown."),
        "days_with_footage": len(found),
        "days_examined": known,
        "oldest_verified_date": oldest.isoformat() if oldest else None,
        "oldest_verified_age_days": (local_now.date() - oldest).days if oldest else None,
        "calendar": calendar_days,
    })
    return result


def previous_attempt_at(previous: object) -> str | None:
    if not isinstance(previous, dict):
        return None
    for key in ("last_attempted_at", "checked_at"):
        value = previous.get(key)
        try:
            checked = datetime.fromisoformat(value)
            if checked.tzinfo is not None:
                return checked.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            pass
    return None


def cached_result(previous: object, track_id: int, now: datetime,
                  days: int, ttl_hours: int, today: date | None = None) -> dict | None:
    """Reuse a recent calendar. With ``today`` (the NVR's local date), a
    calendar is reused only if it was taken today, so the first scan after
    midnight refreshes it and every strip ends yesterday."""
    if not isinstance(previous, dict) or previous.get("track_id") != track_id:
        return None
    if (today is not None and previous.get("status") in {VERIFIED, PARTIAL}
            and previous.get("as_of_date") != today.isoformat()):
        return None
    if previous.get("status") not in {VERIFIED, PARTIAL, UNSUPPORTED}:
        return None
    timestamp = previous_attempt_at(previous)
    if timestamp is None:
        return None
    age = (now - datetime.fromisoformat(timestamp)).total_seconds()
    if not 0 <= age <= ttl_hours * 3600:
        return None
    if previous.get("window_days") != days or not isinstance(previous.get("as_of_date"), str):
        return None
    return dict(previous, from_cache=True)


def deferred_result(previous: object, track_id: int, message: str) -> dict:
    """Keep prior scan visibly stale, and retain fair scheduling timestamp."""
    timestamp = previous_attempt_at(previous)
    if (isinstance(previous, dict) and previous.get("track_id") == track_id
            and previous.get("status") in {VERIFIED, PARTIAL, STALE}):
        result = dict(previous)
        result.update(status=STALE, from_cache=True,
                      message="Earlier recording calendar shown; " + message,
                      last_attempted_at=timestamp)
        return result
    result = empty_result(PENDING, message, track_id)
    result["last_attempted_at"] = timestamp
    return result


def summarize_retention(channels: list[dict], enabled: bool) -> dict:
    statuses = [ch.get("retention_result", {}).get("status") for ch in channels]
    return {
        "enabled": enabled,
        "verified": statuses.count(VERIFIED),
        "partial": statuses.count(PARTIAL),
        "pending": statuses.count(PENDING) + statuses.count(STALE),
        "unsupported": statuses.count(UNSUPPORTED),
        "errors": statuses.count(ERROR) + statuses.count(INCONCLUSIVE),
        "not_configured": statuses.count(NOT_CONFIGURED),
    }
