"""Read-only, sequential per-camera recording checks with verified track mapping.

No NVR endpoint that changes configuration or video files is used here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
import time
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException

from nvr_track.nvr.recording import (
    check_recording_evidence, child_text, local_name, refine_latest_segment,
)
from nvr_track.nvr.retention import (
    scan_calendar, empty_result as empty_retention, cached_result as retention_cache,
    deferred_result as retention_deferred, previous_attempt_at as retention_attempt_at,
    summarize_retention, NOT_CONFIGURED as RET_NOT_CONFIGURED,
    UNVERIFIED as RET_UNVERIFIED, UNSUPPORTED as RET_UNSUPPORTED,
    PENDING as RET_PENDING,
)
from nvr_track.nvr.historical_recording import (
    empty_result, search_history, summarize_history, valid_cache,
    NOT_CHECKED as HIST_NOT_CHECKED, NOT_CONFIGURED as HIST_NOT_CONFIGURED,
)

EVIDENCE = {"RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY"}
INVALID_REPLY_PAUSE_SECONDS = 3
PROBLEMS = {"CHECK_ERROR", "CHECK_INCONCLUSIVE", "TRACK_NOT_FOUND", "TRACK_MAPPING_UNVERIFIED"}


def parse_tracks(content: bytes) -> dict[int, str | None]:
    """Parse track IDs and optional srcChannel; reject duplicate/uncertain data."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Invalid recording-track XML") from exc
    if local_name(root.tag) not in {"tracklist", "tracks", "track"}:
        raise ValueError("Unexpected recording-track XML root")
    tracks = {}
    for node in root.iter():
        if local_name(node.tag) != "track":
            continue
        id_text = child_text(node, "id")
        if not id_text.isdigit() or int(id_text) <= 0:
            raise ValueError("Track contains missing or invalid ID")
        track_id = int(id_text)
        if track_id in tracks:
            raise ValueError("Track list contains duplicate IDs")
        tracks[track_id] = child_text(node, "srcChannel") or None
    if not tracks:
        raise ValueError("No verifiable tracks returned")
    return tracks


def get_tracks(ip: str, port: int, username: str, password: str) -> dict:
    """Read one NVR's track list; never infer absence after a request failure."""
    ip_address(ip)
    url = f"http://{ip}:{port}/ISAPI/ContentMgmt/record/tracks"
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                url, auth=HTTPDigestAuth(username, password),
                headers={"Accept": "application/xml"}, timeout=(3, 8),
                allow_redirects=False,
            )
        if response.status_code != 200:
            status = "PERMISSION_DENIED" if response.status_code in (401, 403) else "UNAVAILABLE"
            return {"status": status, "http_status": response.status_code,
                    "tracks": {}, "message": "Recording-track list could not be verified."}
        try:
            tracks = parse_tracks(response.content)
        except ValueError:
            return {"status": "INVALID_RESPONSE", "http_status": 200,
                    "tracks": {}, "message": "Recording-track response was not verifiable."}
        return {"status": "VERIFIED", "http_status": 200,
                "tracks": tracks, "message": f"Verified {len(tracks)} recording tracks."}
    except RequestException:
        return {"status": "CONNECTION_ERROR", "http_status": None,
                "tracks": {}, "message": "Recording-track request failed or timed out."}


def resolve_track(channel_id: str, tracks: dict[int, str | None]) -> tuple[str, int | None, str]:
    """Require verified presence; never treat missing search matches as a missing track."""
    try:
        channel = int(channel_id)
        if channel <= 0 or str(channel) != str(channel_id):
            raise ValueError
    except (TypeError, ValueError):
        return "TRACK_MAPPING_UNVERIFIED", None, "Channel ID is not a positive integer."

    candidate = channel * 100 + 1
    explicit = [tid for tid, src in tracks.items() if src == str(channel)]
    preferred = [tid for tid in explicit if tid % 100 == 1]
    if len(preferred) == 1:
        return "VERIFIED", preferred[0], "Verified from track source channel and main-stream ID."
    if len(explicit) == 1:
        return "VERIFIED", explicit[0], "Verified from track source channel."
    if explicit:
        return "TRACK_MAPPING_UNVERIFIED", None, "Multiple source tracks; main-stream mapping ambiguous."
    if candidate in tracks:
        source = tracks[candidate]
        if source is not None:
            return "TRACK_MAPPING_UNVERIFIED", None, "Candidate track belongs to a different or unknown source channel."
        return "VERIFIED", candidate, "Verified candidate main-stream ID exists in track list (source field absent)."
    # If source IDs are present but nonnumeric/unexpected, do not invent a mapping.
    return "TRACK_NOT_FOUND", None, "No verified main-stream recording track for this channel."


def get_clock(ip: str, port: int, username: str, password: str) -> tuple[datetime, timezone] | None:
    """Read device-local aware clock once, retaining the verified offset semantics."""
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                f"http://{ip}:{port}/ISAPI/System/time",
                auth=HTTPDigestAuth(username, password),
                headers={"Accept": "application/xml"}, timeout=(3, 8),
                allow_redirects=False,
            )
        if response.status_code != 200:
            return None
        nvr_local = datetime.fromisoformat(
            child_text(ET.fromstring(response.content), "localTime").replace("Z", "+00:00")
        )
        if nvr_local.tzinfo is None:
            return None
        return nvr_local.astimezone(timezone.utc), nvr_local.tzinfo
    except (RequestException, ET.ParseError, ValueError):
        return None


def new_result(status: str, reason: str, track_id: int | None = None, *,
               minutes: int = 60, checked_at: str | None = None) -> dict:
    return {
        "status": status, "message": reason, "track_id": track_id,
        "track_verified": False, "num_matches": None, "total_matches": None,
        "most_recent_segment_start": None, "most_recent_segment_end": None,
        "requested_window_utc": {"start": None, "end": None},
        "checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
        "window_minutes": minutes, "anomalies": [],
        # False: the NVR listed only part of the window, so the newest
        # segment shown is a lower bound. None: not applicable/unknown.
        "latest_segment_complete": None,
    }


def simplify_search(raw: dict, track_id: int, minutes: int) -> dict:
    """Summarize only validated device evidence, without raw search payloads."""
    mapping = {
        "RECORDING_EVIDENCE_FOUND": "RECORDING",
        "RECORDING_EVIDENCE_FOUND_WITH_TIME_ANOMALY": "RECORDING_EVIDENCE_WITH_ANOMALY",
        "NO_MATCHES_IN_WINDOW": "NO_RECENT_RECORDING_EVIDENCE",
        "RECORDING_EVIDENCE_NOT_CONFIRMED": "CHECK_INCONCLUSIVE",
        "UNKNOWN_SEARCH_STATUS": "CHECK_INCONCLUSIVE",
        "INCONSISTENT_OR_UNEXPECTED_RESPONSE": "CHECK_INCONCLUSIVE",
        "NVR_TIME_UNVERIFIED": "CHECK_INCONCLUSIVE",
        "NVR_TIME_UNAVAILABLE": "CHECK_INCONCLUSIVE",
    }
    status = mapping.get(raw.get("status"), "CHECK_ERROR")
    result = new_result(status, str(raw.get("message", "Recording check unavailable.")),
                        track_id, minutes=minutes,
                        checked_at=datetime.now(timezone.utc).isoformat())
    result["track_verified"] = True
    result["num_matches"] = 0 if status in EVIDENCE else raw.get("num_matches")
    result["total_matches"] = raw.get("total_matches")
    result["requested_window_utc"] = raw.get("requested_window_utc") or result["requested_window_utc"]
    result["anomalies"] = raw.get("anomalies", [])[:10]
    window = result["requested_window_utc"]
    matches = []
    for item in raw.get("matches", []):
        if item.get("track_id") != track_id:
            continue
        try:
            start = datetime.fromisoformat(item["start_utc"])
            end = datetime.fromisoformat(item["end_utc"])
            if not (start.tzinfo and end.tzinfo):
                continue
            if not window.get("start") or not window.get("end"):
                matches.append(item)  # unit-level input was already classified by recorder
                continue
            begin_window = datetime.fromisoformat(window["start"])
            end_window = datetime.fromisoformat(window["end"])
            if start <= end_window and end >= begin_window:
                matches.append(item)
        except (ValueError, TypeError, KeyError):
            continue
    if status in EVIDENCE:
        # Evidence is only established by a segment for this track that
        # actually overlaps the requested window. A mismatched reply is
        # inconclusive even if its top-level device status reports success.
        if not matches:
            result["status"] = "CHECK_INCONCLUSIVE"
            result["message"] = "Returned evidence could not be verified for this track and window."
            result["num_matches"] = None
        else:
            result["num_matches"] = len(matches)
            most_recent = max(matches, key=lambda m: datetime.fromisoformat(m["end_utc"]))
            result["most_recent_segment_start"] = most_recent["start_utc"]
            result["most_recent_segment_end"] = most_recent["end_utc"]
            result["more_available"] = bool(raw.get("more_available"))
            result["latest_segment_complete"] = not raw.get("more_available")
    if status == "NO_RECENT_RECORDING_EVIDENCE":
        result["message"] = f"No recording evidence in the last {minutes} minutes; not proof of never recording."
    return result


def summarize(channels: list[dict], enabled: bool, minutes: int, mapping: dict) -> dict:
    rows = [(ch.get("recording_result") or {}) for ch in channels]
    counts = {name: sum(r.get("status") == name for r in rows) for name in (
        "RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY", "NO_RECENT_RECORDING_EVIDENCE",
        "TRACK_NOT_FOUND", "TRACK_MAPPING_UNVERIFIED", "CHECK_INCONCLUSIVE",
        "CHECK_ERROR", "NOT_CHECKED", "NOT_CONFIGURED")}
    offline = [ch for ch in channels if ch.get("reported_status") == "OFFLINE"]
    return {
        "enabled": enabled, "window_minutes": minutes,
        "track_mapping_status": mapping.get("status", "NOT_CONFIGURED"),
        "track_list_count": mapping.get("track_count"),
        "nvr_clock_offset_seconds_approx": mapping.get("nvr_clock_offset_seconds_approx"),
        "total_cameras": len(channels),
        "checked": sum(counts[k] for k in (
            "RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY", "NO_RECENT_RECORDING_EVIDENCE",
            "CHECK_INCONCLUSIVE", "CHECK_ERROR")),
        "evidence": counts["RECORDING"] + counts["RECORDING_EVIDENCE_WITH_ANOMALY"],
        "no_recent_evidence": counts["NO_RECENT_RECORDING_EVIDENCE"],
        "online_missing_expected": sum(
            ch.get("reported_status") == "ONLINE"
            and ch.get("recording_expectation") == "continuous"
            and ch["recording_result"]["status"] == "NO_RECENT_RECORDING_EVIDENCE"
            for ch in channels
        ),
        "missing_expected": sum(
            ch.get("recording_expectation") == "continuous"
            and ch["recording_result"]["status"] == "NO_RECENT_RECORDING_EVIDENCE"
            for ch in channels
        ),
        "offline_with_evidence": sum(ch["recording_result"]["status"] in EVIDENCE for ch in offline),
        "offline_without_recent_evidence": sum(ch["recording_result"]["status"] == "NO_RECENT_RECORDING_EVIDENCE" for ch in offline),
        "track_mapping_problems": counts["TRACK_NOT_FOUND"] + counts["TRACK_MAPPING_UNVERIFIED"],
        "search_errors": counts["CHECK_ERROR"],
        "inconclusive": counts["CHECK_INCONCLUSIVE"],
        "not_checked": counts["NOT_CHECKED"] + counts["NOT_CONFIGURED"],
        "status_counts": counts,
    }


def check_all_cameras(*, ip: str, port: int, username: str, password: str,
                      channels: list[dict], enabled: bool, minutes: int = 60,
                      max_results: int = 10, budget_seconds: int = 300,
                      search=check_recording_evidence, historical_enabled: bool = False,
                      historical_days: int = 7, historical_max_cameras: int = 2,
                      historical_ttl_hours: int = 12,
                      previous_history: dict | None = None,
                      historical_search=check_recording_evidence,
                      retention_enabled: bool = False, retention_days: int = 30,
                      retention_max_cameras: int = 2, retention_ttl_hours: int = 168,
                      previous_retention: dict | None = None,
                      retention_scan=scan_calendar,
                      retry_delay_seconds: int = 15,
                      sleep=time.sleep) -> dict:
    """Process cameras sequentially; searches include offline cameras.

    A request that fails only because the NVR could not be reached (track list,
    clock or one camera search) is retried once after ``retry_delay_seconds``,
    within the time budget. Short network drops then do not appear as
    unverified mappings or search errors. 0 disables the retry.
    """
    mapping = {"status": "NOT_CONFIGURED", "track_count": None}
    if not enabled:
        for ch in channels:
            ch["recording_result"] = new_result("NOT_CONFIGURED", "Per-camera checks not enabled for this NVR.", minutes=minutes)
            ch["historical_recording_result"] = empty_result(HIST_NOT_CONFIGURED, "Per-camera recording monitoring disabled.")
            ch["retention_result"] = empty_retention(RET_NOT_CONFIGURED, "Per-camera recording monitoring disabled.")
        result = summarize(channels, False, minutes, mapping)
        result["retention"] = summarize_retention(channels, False)
        return result

    if not channels:
        mapping["status"] = "CHANNEL_INVENTORY_EMPTY"
        result = summarize(channels, True, minutes, mapping)
        result["retention"] = summarize_retention(channels, retention_enabled)
        return result

    started = time.monotonic()

    def may_retry(pause: int | None = None) -> bool:
        wait = retry_delay_seconds if pause is None else min(pause, retry_delay_seconds)
        return (retry_delay_seconds > 0
                and time.monotonic() - started + wait < budget_seconds)

    tracks_response = get_tracks(ip, port, username, password)
    if tracks_response["status"] == "CONNECTION_ERROR" and may_retry():
        sleep(retry_delay_seconds)
        tracks_response = get_tracks(ip, port, username, password)
    mapping = {"status": tracks_response["status"],
               "track_count": len(tracks_response["tracks"]) if tracks_response["status"] == "VERIFIED" else None}
    if tracks_response["status"] != "VERIFIED":
        for ch in channels:
            ch["recording_result"] = new_result("TRACK_MAPPING_UNVERIFIED", tracks_response["message"], minutes=minutes)
            ch["historical_recording_result"] = empty_result(HIST_NOT_CHECKED, "Recording track mapping unavailable.")
            ch["retention_result"] = empty_retention(
                RET_UNVERIFIED if retention_enabled else RET_NOT_CONFIGURED,
                "Verified track mapping required for retention." if retention_enabled else "Retention monitoring disabled.")
        result = summarize(channels, True, minutes, mapping)
        result["retention"] = summarize_retention(channels, retention_enabled)
        return result

    clock = get_clock(ip, port, username, password)
    if clock is None and may_retry():
        sleep(retry_delay_seconds)
        clock = get_clock(ip, port, username, password)
    clock_read_at = time.monotonic()
    mapping["nvr_clock_offset_seconds_approx"] = (
        round((clock[0] - datetime.now(timezone.utc)).total_seconds())
        if clock is not None else None
    )
    disabled_search_reason = None
    for ch in channels:
        verified, track_id, reason = resolve_track(str(ch.get("channel_id", "")), tracks_response["tracks"])
        if verified != "VERIFIED":
            ch["recording_result"] = new_result(verified, reason, minutes=minutes)
        elif disabled_search_reason:
            ch["recording_result"] = new_result("NOT_CHECKED", disabled_search_reason, track_id, minutes=minutes)
            ch["recording_result"]["track_verified"] = True
        elif time.monotonic() - started >= budget_seconds:
            ch["recording_result"] = new_result("NOT_CHECKED", "NVR per-camera checking time budget exhausted.", track_id, minutes=minutes)
            ch["recording_result"]["track_verified"] = True
        elif clock is None:
            ch["recording_result"] = new_result("CHECK_INCONCLUSIVE", "NVR clock could not be verified.", track_id, minutes=minutes)
            ch["recording_result"]["track_verified"] = True
        else:
            # All calls complete in order: never overlap recording searches.
            def run_search(track_id=track_id):
                elapsed = timedelta(seconds=time.monotonic() - clock_read_at)
                return search(ip=ip, username=username, password=password, port=port,
                              track_id=track_id, minutes=minutes, max_results=max_results,
                              clock_snapshot=(clock[0] + elapsed, clock[1]))
            try:
                raw = run_search()
                retried = False
                if raw.get("status") == "CONNECTION_OR_REQUEST_ERROR" and may_retry():
                    sleep(retry_delay_seconds)
                    raw = run_search()
                    retried = True
                # A reply that fails validation is often caught mid-write (a
                # segment just opening). Ask once more after a short pause.
                if raw.get("status") == "INCONSISTENT_OR_UNEXPECTED_RESPONSE" and may_retry(INVALID_REPLY_PAUSE_SECONDS):
                    sleep(min(INVALID_REPLY_PAUSE_SECONDS, retry_delay_seconds))
                    raw = run_search()
                    ch_retry_reason = "invalid_reply"
                else:
                    ch_retry_reason = None
                ch["recording_result"] = simplify_search(raw, track_id, minutes)
                if ch["recording_result"].get("latest_segment_complete") is False:
                    ch["recording_result"]["_raw"] = raw  # Refined after all cameras.
                if retried:
                    ch["recording_result"]["retried_after_connection_error"] = True
                if ch_retry_reason:
                    ch["recording_result"]["retried_after_invalid_reply"] = True
                if raw.get("status") in {"AUTHENTICATION_FAILED", "PERMISSION_DENIED", "ENDPOINT_UNAVAILABLE"}:
                    disabled_search_reason = "Subsequent searches skipped after endpoint/authentication failure."
            except Exception:
                # A programming or unexpected HTTP error must not abort the whole report.
                ch["recording_result"] = new_result(
                    "CHECK_ERROR", "Unexpected per-camera recording check failure.",
                    track_id, minutes=minutes, checked_at=datetime.now(timezone.utc).isoformat(),
                )
                ch["recording_result"]["track_verified"] = True
    # Second pass, only with budget left after every camera has its primary
    # result: a truncated (MORE) reply lists the oldest segments first, so
    # find the newest one. This never changes a camera's recording status.
    for ch in channels:
        recent = ch["recording_result"]
        raw_first = recent.pop("_raw", None)
        if (raw_first is None or recent.get("latest_segment_complete") is not False
                or time.monotonic() - started >= budget_seconds):
            continue
        track_id = recent["track_id"]

        def search_window(low, high, track_id=track_id):
            elapsed = timedelta(seconds=time.monotonic() - clock_read_at)
            return search(ip=ip, username=username, password=password, port=port,
                          track_id=track_id, minutes=minutes, max_results=max_results,
                          clock_snapshot=(clock[0] + elapsed, clock[1]),
                          window_start_utc=low, window_end_utc=high)
        try:
            refined = refine_latest_segment(
                raw_first, track_id=track_id, search_window=search_window,
                max_searches=6, deadline=started + budget_seconds,
            )
        except Exception:
            refined = None
        if refined is None:
            continue
        if (recent.get("most_recent_segment_end") is None
                or datetime.fromisoformat(refined["end_utc"])
                > datetime.fromisoformat(recent["most_recent_segment_end"])):
            recent["most_recent_segment_start"] = refined["start_utc"]
            recent["most_recent_segment_end"] = refined["end_utc"]
        recent["latest_segment_complete"] = refined["complete"]
        recent["latest_segment_searches"] = refined["searches"]
    for ch in channels:
        ch["recording_result"].pop("_raw", None)

    history_budget = time.monotonic() + max(0, budget_seconds - (time.monotonic() - started))
    previous_history = previous_history or {}
    pending_history = []
    # First retain valid cached results and handle ineligible cameras. Defer
    # new searches until we can prioritize cameras across the entire NVR.
    for ch in channels:
        recent = ch["recording_result"]
        track_id = recent.get("track_id") if recent.get("track_verified") else None
        if not historical_enabled:
            historic = empty_result(HIST_NOT_CONFIGURED, "Historical monitoring disabled.", track_id)
        elif track_id is None or clock is None:
            historic = empty_result(HIST_NOT_CHECKED, "Historical check requires a verified track and device clock.", track_id)
        else:
            device_now = clock[0] + timedelta(seconds=time.monotonic() - clock_read_at)
            old = previous_history.get(str(ch.get("channel_id")))
            historic = valid_cache(
                old, track_id, now=datetime.now(timezone.utc),
                ttl_hours=historical_ttl_hours,
                oldest=device_now - timedelta(days=historical_days),
            )
            if historic is None and recent["status"] != "NO_RECENT_RECORDING_EVIDENCE":
                historic = empty_result(HIST_NOT_CHECKED, "Historical searches prioritize cameras lacking recent recording evidence.", track_id)
            elif historic is None:
                pending_history.append((ch, track_id, device_now, old))
                continue
        ch["historical_recording_result"] = historic

    def previous_attempt_at(previous: object) -> str | None:
        """Retain the last *actual* search time through deferred reports.

        The cache can expire between daily runs. A deferred camera must not
        lose its prior check age and subsequently outrank never-checked ones.
        """
        if not isinstance(previous, dict):
            return None
        for field in ("checked_at", "last_attempted_at"):
            value = previous.get(field)
            if not isinstance(value, str):
                continue
            try:
                checked = datetime.fromisoformat(value)
                if checked.tzinfo is not None:
                    return checked.astimezone(timezone.utc).isoformat()
            except (ValueError, OverflowError):
                continue
        return None

    def historical_priority(candidate: tuple) -> tuple[int, float]:
        """Search never-checked cameras before those with an older attempt."""
        last_attempt = previous_attempt_at(candidate[3])
        if last_attempt is None:
            return (0, 0.0)
        return (1, datetime.fromisoformat(last_attempt).timestamp())

    history_checked = 0
    for ch, track_id, device_now, old in sorted(pending_history, key=historical_priority):
        if history_checked >= historical_max_cameras:
            historic = empty_result(HIST_NOT_CHECKED, "Historical per-report camera limit reached.", track_id)
        elif time.monotonic() >= history_budget:
            historic = empty_result(HIST_NOT_CHECKED, "NVR recording-search time budget exhausted.", track_id)
        else:
            history_checked += 1
            historic = search_history(
                ip=ip, port=port, username=username, password=password,
                track_id=track_id, clock_snapshot=(device_now, clock[1]),
                recent_minutes=minutes, lookback_days=historical_days,
                max_results=max_results, deadline=history_budget,
                search=historical_search,
            )
        if historic["status"] == HIST_NOT_CHECKED:
            # `checked_at` remains None: no new request was made. This separate
            # field preserves fair ordering on subsequent 24-hour cron runs.
            historic["last_attempted_at"] = previous_attempt_at(old)
        ch["historical_recording_result"] = historic
    # Separate, optional calendar audit for *all* camera statuses. Its small
    # independent budget cannot increase the historical-search request limit.
    previous_retention = previous_retention or {}
    pending_retention = []
    for ch in channels:
        recent = ch["recording_result"]
        track_id = recent.get("track_id") if recent.get("track_verified") else None
        if not retention_enabled:
            retention = empty_retention(RET_NOT_CONFIGURED, "Retention monitoring disabled.", track_id)
        elif track_id is None or clock is None:
            retention = empty_retention(RET_UNVERIFIED, "Verified track and device clock required.", track_id)
        else:
            old = previous_retention.get(str(ch.get("channel_id")))
            device_today = (clock[0] + timedelta(seconds=time.monotonic() - clock_read_at)
                            ).astimezone(clock[1]).date()
            retention = retention_cache(old, track_id, datetime.now(timezone.utc),
                                        retention_days, retention_ttl_hours, today=device_today)
            if retention is None:
                pending_retention.append((ch, track_id, old))
                continue
        ch["retention_result"] = retention

    def retention_priority(candidate: tuple) -> tuple[int, float]:
        attempt = retention_attempt_at(candidate[2])
        return (1, datetime.fromisoformat(attempt).timestamp()) if attempt else (0, 0.0)

    retention_count = 0
    skip_remaining = False
    for ch, track_id, old in sorted(pending_retention, key=retention_priority):
        if skip_remaining:
            retention = retention_deferred(old, track_id, "Calendar endpoint unavailable on this NVR.")
        elif retention_count >= retention_max_cameras:
            retention = retention_deferred(old, track_id, "Per-report retention camera limit reached.")
        elif time.monotonic() - started >= budget_seconds:
            retention = retention_deferred(old, track_id, "NVR recording-search time budget exhausted.")
        else:
            retention_count += 1
            device_now = clock[0] + timedelta(seconds=time.monotonic() - clock_read_at)
            try:
                retention = retention_scan(
                    ip=ip, port=port, username=username, password=password,
                    track_id=track_id, clock_snapshot=(device_now, clock[1]),
                    days=retention_days, deadline=started + budget_seconds,
                )
            except Exception:
                retention = empty_retention("RETENTION_ERROR", "Unexpected calendar audit failure.", track_id)
            if retention["status"] == RET_PENDING:
                # The budget ran out between monthly queries. Keep the earlier
                # calendar (shown as a previous scan), like any deferred camera.
                retention = retention_deferred(old, track_id, "NVR recording-search time budget exhausted.")
            if retention["status"] == RET_UNSUPPORTED:
                skip_remaining = True
        ch["retention_result"] = retention

    summary = summarize(channels, True, minutes, mapping)
    summary["historical"] = summarize_history(channels, historical_enabled)
    summary["retention"] = summarize_retention(channels, retention_enabled)
    return summary
