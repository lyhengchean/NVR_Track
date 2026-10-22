"""Read-only Hikvision recording search with explicit anomaly classification."""

from datetime import datetime, timedelta, timezone, tzinfo
from ipaddress import ip_address
import uuid
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException


API_NS = "http://www.isapi.org/ver20/XMLSchema"


def qname(name: str) -> str:
    return f"{{{API_NS}}}{name}"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(root: ET.Element, name: str) -> str:
    wanted = name.lower()
    for node in root.iter():
        if local_name(node.tag) == wanted and node.text:
            value = node.text.strip()
            if value:
                return value
    return ""


def parse_timestamp(value: str) -> datetime:
    """Parse a timestamp with a timezone offset and normalize to UTC."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        raise ValueError("Timestamp has no timezone offset.")

    return parsed.astimezone(timezone.utc)

def parse_search_timestamp(
    value: str,
    timestamp_timezone: tzinfo | None = None,
) -> datetime:
    """Parse a search timestamp, optionally treating Z as NVR-local time."""

    text = value.strip()

    # Some NVR search responses appear to append Z to local wall-clock times.
    # Enable this interpretation only when the caller supplies the NVR timezone.
    if timestamp_timezone is not None and text.endswith("Z"):
        wall_time = datetime.fromisoformat(text[:-1])

        if wall_time.tzinfo is not None:
            raise ValueError("Expected a timezone-naive wall-clock timestamp.")

        return wall_time.replace(
            tzinfo=timestamp_timezone
        ).astimezone(timezone.utc)

    return parse_timestamp(text)

def make_search_xml(
    track_id: int,
    start_utc: datetime,
    end_utc: datetime,
    max_results: int,
    *,
    request_timezone: tzinfo | None = None,
) -> bytes:
    """Build the legacy recording-search request."""

    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValueError("Search times must include timezone information.")

    if end_utc <= start_utc:
        raise ValueError("Search end time must follow start time.")

    if isinstance(track_id, bool) or not isinstance(track_id, int) or track_id <= 0:
        raise ValueError("Track ID must be a positive integer.")

    if (
        isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or not 1 <= max_results <= 64
    ):
        raise ValueError("Maximum results must be between 1 and 64.")

    wire_timezone = (
        request_timezone
        if request_timezone is not None
        else timezone.utc
    )

    request_start = start_utc.astimezone(wire_timezone)
    request_end = end_utc.astimezone(wire_timezone)

    root = ET.Element("CMSearchDescription")
    ET.SubElement(root, "searchID").text = str(uuid.uuid4()).upper()

    track_list = ET.SubElement(root, "trackList")
    ET.SubElement(track_list, "trackID").text = str(track_id)

    time_span_list = ET.SubElement(root, "timeSpanList")
    time_span = ET.SubElement(time_span_list, "timeSpan")

    ET.SubElement(time_span, "startTime").text = (
        request_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    ET.SubElement(time_span, "endTime").text = (
        request_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    ET.SubElement(root, "maxResults").text = str(max_results)
    ET.SubElement(root, "searchResultPostion").text = "0"

    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )

def parse_search_response(
    content: bytes,
    *,
    timestamp_timezone: tzinfo | None = None,
) -> dict:
    """Parse search metadata while preserving an absent totalMatches as None."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Search response is not valid XML.") from exc

    if local_name(root.tag) != "cmsearchresult":
        raise ValueError("Unexpected recording-search XML root.")

    response_status = child_text(root, "responseStatus").lower()
    status_string = child_text(root, "responseStatusStrg").upper()
    num_text = child_text(root, "numOfMatches")
    total_text = child_text(root, "totalMatches")

    if response_status not in {"true", "false"}:
        raise ValueError("Missing or invalid responseStatus.")

    if not num_text:
        raise ValueError("Missing numOfMatches.")

    try:
        num_matches = int(num_text)
        total_matches = int(total_text) if total_text else None
    except ValueError as exc:
        raise ValueError("Invalid search match count.") from exc

    if num_matches < 0 or (
        total_matches is not None and total_matches < 0
    ):
        raise ValueError("Search counts cannot be negative.")

    matches = []

    for node in root.iter():
        if local_name(node.tag) != "searchmatchitem":
            continue

        raw_track = child_text(node, "trackID")
        raw_start = child_text(node, "startTime")
        raw_end = child_text(node, "endTime")

        if not raw_track or not raw_start or not raw_end:
            raise ValueError("A search result lacks required fields.")

        try:
            track = int(raw_track)
            start = parse_search_timestamp(raw_start, timestamp_timezone)
            end = parse_search_timestamp(raw_end, timestamp_timezone)
        except (ValueError, OverflowError) as exc:
            raise ValueError("A search result contains invalid values.") from exc

        if track <= 0 or end <= start:
            raise ValueError("A search result has an invalid track or time range.")

        matches.append({
            "track_id": track,
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
            "_start": start,
            "_end": end,
        })

    if num_matches != len(matches):
        raise ValueError(
            "numOfMatches differs from the number of returned match entries."
        )

    if total_matches is not None and total_matches < num_matches:
        raise ValueError("totalMatches is smaller than numOfMatches.")

    return {
        "response_status": response_status,
        "search_status": status_string,
        "num_matches": num_matches,
        "total_matches": total_matches,
        "matches": matches,
    }


def _result(
    status: str,
    message: str,
    *,
    track_id: int,
    http_status: int | None = None,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    num_matches: int | None = None,
    total_matches: int | None = None,
    matches: list | None = None,
    anomalies: list[str] | None = None,
) -> dict:
    """Create a JSON-friendly search result."""
    return {
        "status": status,
        "message": message,
        "http_status": http_status,
        "track_id": track_id,
        "requested_window_utc": {
            "start": start_utc.isoformat() if start_utc else None,
            "end": end_utc.isoformat() if end_utc else None,
        },
        "num_matches": num_matches,
        "total_matches": total_matches,
        "matches": matches or [],
        "anomalies": anomalies or [],
    }


def check_recording_evidence(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    track_id: int = 201,
    minutes: int = 60,
    max_results: int = 10,
) -> dict:
    """Search a recent time window and classify the result conservatively."""
    try:
        address = ip_address(ip)

        if address.version != 4:
            raise ValueError("IPv4 is required.")

        if isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("Invalid TCP port.")

        if isinstance(track_id, bool) or track_id <= 0:
            raise ValueError("Track ID must be positive.")

        if isinstance(minutes, bool) or not 1 <= minutes <= 1440:
            raise ValueError("Minutes must be between 1 and 1440.")

        if isinstance(max_results, bool) or not 1 <= max_results <= 64:
            raise ValueError("Maximum results must be between 1 and 64.")

    except ValueError:
        return _result(
            "INVALID_CONFIGURATION",
            "Invalid IP address, port, track ID, or search limits.",
            track_id=track_id,
        )

    if not username or not password:
        return _result(
            "INVALID_CONFIGURATION",
            "A username and password are required.",
            track_id=track_id,
        )

    base_url = f"http://{address}:{port}"
    auth = HTTPDigestAuth(username, password)

    try:
        with requests.Session() as session:
            session.trust_env = False

            clock_response = session.get(
                f"{base_url}/ISAPI/System/time",
                auth=auth,
                headers={"Accept": "application/xml"},
                timeout=(3, 8),
                allow_redirects=False,
            )

            if clock_response.status_code in (401, 403):
                status = (
                    "AUTHENTICATION_FAILED"
                    if clock_response.status_code == 401
                    else "PERMISSION_DENIED"
                )
                return _result(
                    status,
                    "NVR time request was not authorized.",
                    track_id=track_id,
                    http_status=clock_response.status_code,
                )

            if clock_response.status_code != 200:
                return _result(
                    "NVR_TIME_UNAVAILABLE",
                    "NVR time could not be retrieved.",
                    track_id=track_id,
                    http_status=clock_response.status_code,
                )

            try:
                clock_root = ET.fromstring(clock_response.content)

                raw_local_time = child_text(clock_root, "localTime")
                nvr_local = datetime.fromisoformat(
                    raw_local_time.strip().replace("Z", "+00:00")
                )

                if nvr_local.tzinfo is None:
                    raise ValueError("NVR clock has no timezone offset.")

                nvr_timezone = nvr_local.tzinfo
                nvr_now = nvr_local.astimezone(timezone.utc)

            except (ET.ParseError, ValueError):
                return _result(
                    "NVR_TIME_UNVERIFIED",
                    "NVR time was not valid timezone-aware XML.",
                    track_id=track_id,
                )

            end_utc = nvr_now.replace(microsecond=0)
            start_utc = end_utc - timedelta(minutes=minutes)

            payload = make_search_xml(
                track_id,
                start_utc,
                end_utc,
                max_results,
                request_timezone=nvr_timezone,
            )

            response = session.post(
                f"{base_url}/ISAPI/ContentMgmt/search",
                auth=auth,
                data=payload,
                headers={
                    "Content-Type": "application/xml; charset=UTF-8",
                    "Accept": "application/xml",
                },
                timeout=(3, 15),
                allow_redirects=False,
            )

            if response.status_code == 401:
                return _result(
                    "AUTHENTICATION_FAILED",
                    "Recording-search authentication failed.",
                    track_id=track_id,
                    http_status=response.status_code,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )

            if response.status_code == 403:
                return _result(
                    "PERMISSION_DENIED",
                    "The account lacks recording-search permission.",
                    track_id=track_id,
                    http_status=response.status_code,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )

            if response.status_code == 404:
                return _result(
                    "ENDPOINT_UNAVAILABLE",
                    "The recording-search endpoint was not found.",
                    track_id=track_id,
                    http_status=response.status_code,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )

            if response.status_code != 200:
                detail = f"HTTP {response.status_code}"

                try:
                    error_root = ET.fromstring(response.content)

                    if local_name(error_root.tag) == "responsestatus":
                        fields = {
                            "subStatusCode": child_text(
                                error_root, "subStatusCode"
                            ),
                            "statusString": child_text(
                                error_root, "statusString"
                            ),
                            "errorCode": child_text(
                                error_root, "errorCode"
                            ),
                        }

                        for name, value in fields.items():
                            if value:
                                detail += f"; {name}={value}"
                    else:
                        detail += (
                            "; unexpected XML root="
                            f"{local_name(error_root.tag)}"
                        )

                except ET.ParseError:
                    detail += (
                        "; error response was not parseable XML"
                    )

                return _result(
                    "SEARCH_REQUEST_FAILED",
                    f"Recording search failed: {detail}",
                    track_id=track_id,
                    http_status=response.status_code,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )

                        # Parse the successful HTTP response before inspecting its root.
            try:
                response_root = ET.fromstring(response.content)
            except ET.ParseError:
                return _result(
                    "INCONSISTENT_OR_UNEXPECTED_RESPONSE",
                    "Recording-search response is not valid XML.",
                    track_id=track_id,
                    http_status=response.status_code,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )

            root_name = local_name(response_root.tag)

            if root_name == "responsestatus":
                sub_status = child_text(response_root, "subStatusCode")
                status_string = child_text(response_root, "statusString")

                return _result(
                    "DEVICE_REPORTED_SEARCH_ERROR",
                    (
                        "NVR rejected the recording query. "
                        f"Substatus: {sub_status or 'NOT_RETURNED'}; "
                        f"message: {status_string or 'NOT_RETURNED'}."
                    ),
                    track_id=track_id,
                    http_status=response.status_code,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )

            try:
                parsed = parse_search_response(
                    response.content,
                    timestamp_timezone=nvr_timezone,
                )
            except ValueError:
                return _result(
                    "INCONSISTENT_OR_UNEXPECTED_RESPONSE",
                    "Recording-search response could not be validated.",
                    track_id=track_id,
                    http_status=response.status_code,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )

            shared = {
                "track_id": track_id,
                "http_status": response.status_code,
                "start_utc": start_utc,
                "end_utc": end_utc,
                "num_matches": parsed["num_matches"],
                "total_matches": parsed["total_matches"],
            }

            response_status = parsed["response_status"]
            search_status = parsed["search_status"]
            matches = parsed["matches"]

            if (
                response_status == "false"
                and search_status in {"NO MATCHES", "NO MATCH"}
                and not matches
            ):
                return _result(
                    "NO_MATCHES_IN_WINDOW",
                    "The search completed without matching recording segments.",
                    matches=[],
                    **shared,
                )

            if response_status != "true":
                return _result(
                    "DEVICE_REPORTED_SEARCH_ERROR",
                    f"NVR search status was {search_status or 'NOT_RETURNED'}.",
                    matches=matches,
                    **shared,
                )

            if search_status not in {"OK", "MORE"}:
                return _result(
                    "UNKNOWN_SEARCH_STATUS",
                    f"Unrecognized successful response status: {search_status}.",
                    matches=matches,
                    **shared,
                )

            if not matches:
                return _result(
                    "NO_MATCHES_IN_WINDOW",
                    "The successful search returned no recording segments.",
                    **shared,
                )

            anomalies = []
            verified_overlaps = 0

            for match in matches:
                match_start = match.pop("_start")
                match_end = match.pop("_end")

                if match["track_id"] != track_id:
                    anomalies.append("RETURNED_TRACK_DOES_NOT_MATCH")
                    continue

                overlaps = (
                    match_start <= end_utc
                    and match_end >= start_utc
                )

                if not overlaps:
                    anomalies.append("SEGMENT_DOES_NOT_OVERLAP_WINDOW")
                    continue

                verified_overlaps += 1

                if match_end > nvr_now + timedelta(minutes=2):
                    anomalies.append("SEGMENT_END_AFTER_NVR_CLOCK")

            if verified_overlaps == 0:
                return _result(
                    "RECORDING_EVIDENCE_NOT_CONFIRMED",
                    "No returned segment could be verified for the requested track and window.",
                    matches=matches,
                    anomalies=anomalies,
                    **shared,
                )

            if anomalies:
                return _result(
                    "RECORDING_EVIDENCE_FOUND_WITH_TIME_ANOMALY",
                    (
                        "A segment overlaps the requested interval, "
                        "but at least one result needs further verification."
                    ),
                    matches=matches,
                    anomalies=sorted(set(anomalies)),
                    **shared,
                )

            message = "Recording segment evidence overlaps the requested interval."
            if search_status == "MORE":
                message += " More search results are available."

            return _result(
                "RECORDING_EVIDENCE_FOUND",
                message,
                matches=matches,
                **shared,
            )

    except RequestException:
        return _result(
            "CONNECTION_OR_REQUEST_ERROR",
            "The recording-search request failed or timed out.",
            track_id=track_id,
        )