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

from app.recording import check_recording_evidence, child_text, local_name

EVIDENCE = {"RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY"}
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
            most_recent = max(matches, key=lambda m: m["end_utc"])
            result["most_recent_segment_start"] = most_recent["start_utc"]
            result["most_recent_segment_end"] = most_recent["end_utc"]
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
        "total_cameras": len(channels),
        "checked": sum(counts[k] for k in (
            "RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY", "NO_RECENT_RECORDING_EVIDENCE",
            "CHECK_INCONCLUSIVE", "CHECK_ERROR")),
        "evidence": counts["RECORDING"] + counts["RECORDING_EVIDENCE_WITH_ANOMALY"],
        "no_recent_evidence": counts["NO_RECENT_RECORDING_EVIDENCE"],
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
                      search=check_recording_evidence) -> dict:
    """Process cameras sequentially; searches include offline cameras."""
    mapping = {"status": "NOT_CONFIGURED", "track_count": None}
    if not enabled:
        for ch in channels:
            ch["recording_result"] = new_result("NOT_CONFIGURED", "Per-camera checks not enabled for this NVR.", minutes=minutes)
        return summarize(channels, False, minutes, mapping)

    if not channels:
        mapping["status"] = "CHANNEL_INVENTORY_EMPTY"
        return summarize(channels, True, minutes, mapping)

    started = time.monotonic()
    tracks_response = get_tracks(ip, port, username, password)
    mapping = {"status": tracks_response["status"],
               "track_count": len(tracks_response["tracks"]) if tracks_response["status"] == "VERIFIED" else None}
    if tracks_response["status"] != "VERIFIED":
        for ch in channels:
            ch["recording_result"] = new_result("TRACK_MAPPING_UNVERIFIED", tracks_response["message"], minutes=minutes)
        return summarize(channels, True, minutes, mapping)

    clock = get_clock(ip, port, username, password)
    clock_read_at = time.monotonic()
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
            elapsed = timedelta(seconds=time.monotonic() - clock_read_at)
            try:
                raw = search(ip=ip, username=username, password=password, port=port,
                             track_id=track_id, minutes=minutes, max_results=max_results,
                             clock_snapshot=(clock[0] + elapsed, clock[1]))
                ch["recording_result"] = simplify_search(raw, track_id, minutes)
                if raw.get("status") in {"AUTHENTICATION_FAILED", "PERMISSION_DENIED", "ENDPOINT_UNAVAILABLE"}:
                    disabled_search_reason = "Subsequent searches skipped after endpoint/authentication failure."
            except Exception:
                # A programming or unexpected HTTP error must not abort the whole report.
                ch["recording_result"] = new_result(
                    "CHECK_ERROR", "Unexpected per-camera recording check failure.",
                    track_id, minutes=minutes, checked_at=datetime.now(timezone.utc).isoformat(),
                )
                ch["recording_result"]["track_verified"] = True
    return summarize(channels, True, minutes, mapping)
