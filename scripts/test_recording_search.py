"""Read-only Hikvision recording search with result validation."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from getpass import getpass
import ipaddress
import uuid
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException


API_NS = "http://www.isapi.org/ver20/XMLSchema"
ET.register_namespace("", API_NS)


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
    """Parse a timestamp with an explicit offset and normalize to UTC."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Recording timestamp has no timezone offset.")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class SearchMatch:
    track_id: int
    start: datetime
    end: datetime


@dataclass(frozen=True)
class SearchResponse:
    response_status: str
    status_string: str
    num_matches: int
    total_matches: int | None
    matches: tuple[SearchMatch, ...]


def parse_search_response(content: bytes) -> SearchResponse:
    """Parse a CMSearchResult without inventing absent count fields."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Response is not valid XML.") from exc

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
        raise ValueError("Invalid search result count.") from exc

    if num_matches < 0 or (
        total_matches is not None and total_matches < 0
    ):
        raise ValueError("Search result counts cannot be negative.")

    match_nodes = [
        node for node in root.iter()
        if local_name(node.tag) == "searchmatchitem"
    ]

    matches = []

    for node in match_nodes:
        raw_track = child_text(node, "trackID")
        raw_start = child_text(node, "startTime")
        raw_end = child_text(node, "endTime")

        if not raw_track or not raw_start or not raw_end:
            raise ValueError("A recording match lacks required fields.")

        try:
            track_id = int(raw_track)
            start = parse_timestamp(raw_start)
            end = parse_timestamp(raw_end)
        except (ValueError, OverflowError) as exc:
            raise ValueError("A recording match contains invalid values.") from exc

        if track_id <= 0 or end <= start:
            raise ValueError("A recording match has an invalid track or time range.")

        matches.append(SearchMatch(track_id, start, end))

    if num_matches != len(matches):
        raise ValueError(
            "numOfMatches does not equal the number of returned match entries."
        )

    if total_matches is not None and total_matches < num_matches:
        raise ValueError("totalMatches is smaller than numOfMatches.")

    return SearchResponse(
        response_status=response_status,
        status_string=status_string,
        num_matches=num_matches,
        total_matches=total_matches,
        matches=tuple(matches),
    )


def get_nvr_clock(
    session: requests.Session,
    base_url: str,
    auth: HTTPDigestAuth,
) -> datetime:
    response = session.get(
        f"{base_url}/ISAPI/System/time",
        auth=auth,
        headers={"Accept": "application/xml"},
        timeout=(3, 8),
        allow_redirects=False,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"NVR time request returned HTTP {response.status_code}."
        )

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise RuntimeError("NVR time response was not valid XML.") from exc

    raw_time = child_text(root, "localTime")
    if not raw_time:
        raise RuntimeError("NVR did not return localTime.")

    try:
        return parse_timestamp(raw_time)
    except ValueError as exc:
        raise RuntimeError("NVR time did not contain a valid timezone.") from exc


def make_search_xml(
    track_id: int,
    start_utc: datetime,
    end_utc: datetime,
    max_results: int,
) -> bytes:
    """Build a namespaced CMSearchDescription request."""
    root = ET.Element(qname("CMSearchDescription"), {"version": "2.0"})
    ET.SubElement(root, qname("searchID")).text = str(uuid.uuid4()).upper()

    track_list = ET.SubElement(root, qname("trackIDList"))
    ET.SubElement(track_list, qname("trackID")).text = str(track_id)

    spans = ET.SubElement(root, qname("timeSpanList"))
    span = ET.SubElement(spans, qname("timeSpan"))
    ET.SubElement(span, qname("startTime")).text = (
        start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    ET.SubElement(span, qname("endTime")).text = (
        end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    ET.SubElement(root, qname("maxResults")).text = str(max_results)
    ET.SubElement(root, qname("searchResultPosition")).text = "0"

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", default="10.233.220.51")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--track-id", type=int, default=201)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--max-results", type=int, default=10)
    args = parser.parse_args()

    try:
        address = str(ipaddress.ip_address(args.ip))
        if ipaddress.ip_address(address).version != 4:
            raise ValueError("IPv4 is required.")
        if not 1 <= args.port <= 65535:
            raise ValueError("Invalid port.")
        if args.track_id <= 0:
            raise ValueError("Track ID must be positive.")
        if not 1 <= args.minutes <= 1440:
            raise ValueError("Minutes must be between 1 and 1440.")
        if not 1 <= args.max_results <= 64:
            raise ValueError("Maximum results must be between 1 and 64.")
    except ValueError as exc:
        print(f"Invalid argument: {exc}")
        return 2

    username = input("NVR username [admin]: ").strip() or "admin"
    password = getpass("Password (input hidden): ")
    if not password:
        print("Password cannot be empty.")
        return 2

    base_url = f"http://{address}:{args.port}"
    auth = HTTPDigestAuth(username, password)

    try:
        with requests.Session() as session:
            session.trust_env = False

            # Use the recorder's own clock to bound the search.
            nvr_now = get_nvr_clock(session, base_url, auth)
            end_utc = nvr_now.replace(microsecond=0)
            start_utc = end_utc - timedelta(minutes=args.minutes)

            payload = make_search_xml(
                args.track_id, start_utc, end_utc, args.max_results
            )

            print(f"\nNVR: {address}:{args.port}")
            print(f"Track ID: {args.track_id}")
            print(f"Search window: {args.minutes} minutes")
            print(f"Start UTC: {start_utc:%Y-%m-%dT%H:%M:%SZ}")
            print(f"End UTC:   {end_utc:%Y-%m-%dT%H:%M:%SZ}")

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

            print(f"HTTP status: {response.status_code}")

            if response.status_code == 401:
                print("Result: AUTHENTICATION_FAILED")
                return 2
            if response.status_code == 403:
                print("Result: PERMISSION_DENIED")
                return 2
            if response.status_code != 200:
                print("Result: SEARCH_REQUEST_FAILED")
                return 2

            try:
                result = parse_search_response(response.content)
            except ValueError as exc:
                print("Result: INCONSISTENT_OR_UNEXPECTED_RESPONSE")
                print(f"Reason: {exc}")
                return 2

            print(f"Response status: {result.response_status}")
            print(f"Search status: {result.status_string}")
            print(f"Matches returned: {result.num_matches}")
            print(
                "Total matches reported: "
                f"{result.total_matches if result.total_matches is not None else 'NOT_RETURNED'}"
            )

            if (
                result.response_status == "false"
                and result.status_string in {"NO MATCHES", "NO MATCH"}
                and result.num_matches == 0
            ):
                print("Result: NO_MATCHES_IN_WINDOW")
                return 0

            if result.response_status != "true":
                print("Result: DEVICE_REPORTED_SEARCH_FAILURE")
                return 2

            if result.status_string not in {"OK", "MORE"}:
                print("Result: UNKNOWN_SEARCH_STATUS")
                return 2

            if not result.matches:
                print("Result: INCONSISTENT_RESPONSE")
                return 2

            now_tolerance = nvr_now + timedelta(minutes=2)
            verified_overlaps = 0
            time_anomaly = False

            for match in result.matches:
                print(
                    f"Match: track={match.track_id}"
                    f" | start={match.start:%Y-%m-%dT%H:%M:%SZ}"
                    f" | end={match.end:%Y-%m-%dT%H:%M:%SZ}"
                )

                correct_track = match.track_id == args.track_id
                overlaps = (
                    match.start <= end_utc
                    and match.end >= start_utc
                )
                future_end = match.end > now_tolerance

                if not correct_track:
                    print("  Warning: returned track differs from requested track.")
                    time_anomaly = True

                if not overlaps:
                    print("  Warning: returned segment does not overlap the requested window.")
                    time_anomaly = True

                if future_end:
                    print("  Warning: segment end is beyond the NVR clock.")
                    time_anomaly = True

                if correct_track and overlaps and not future_end:
                    verified_overlaps += 1

            if time_anomaly:
                print("Result: RECORDING_EVIDENCE_NEEDS_TIME_VERIFICATION")
                print("Compare these intervals with Playback on the NVR.")
                return 2

            if verified_overlaps:
                print("Result: RECORDING_EVIDENCE_FOUND")
                print("Recording health: EVIDENCE_FOUND_FOR_THIS_TRACK_AND_WINDOW")
                return 0

            print("Result: RECORDING_EVIDENCE_NOT_CONFIRMED")
            return 2

    except (RequestException, RuntimeError, ValueError) as exc:
        # Avoid printing request headers, request bodies, or credentials.
        print(f"Recording search could not be verified: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())