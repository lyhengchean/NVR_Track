"""Read-only inspection of NVR HDDs, recording tracks and clock."""

from getpass import getpass
import re
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException


NVR_IP = ""  # Asked in main().
PORT = 80

ENDPOINTS = [
    ("Storage summary", "/ISAPI/ContentMgmt/Storage"),
    ("HDD list", "/ISAPI/ContentMgmt/Storage/hdd"),
    ("Recording search profile", "/ISAPI/ContentMgmt/search/profile"),
    ("Recording tracks", "/ISAPI/ContentMgmt/record/tracks"),
    ("NVR time", "/ISAPI/System/time"),
]


def ask_ip() -> str:
    """The NVR to check, typed each time: no NVR address is kept in the code."""
    ip = input("NVR IPv4: ").strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
        raise SystemExit("Enter the NVR's IPv4 address, e.g. 192.0.2.10.")
    return ip


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element: ET.Element, *names: str) -> str:
    """Find the first non-empty matching element below the given node."""
    wanted = {name.lower() for name in names}

    for node in element.iter():
        if local_name(node.tag) in wanted and node.text:
            value = node.text.strip()
            if value:
                return value

    return ""


def fetch_xml(
    session: requests.Session,
    auth: HTTPDigestAuth,
    label: str,
    path: str,
) -> ET.Element | None:
    print(f"\n========== {label} ==========")
    print(f"GET {path}")

    try:
        response = session.get(
            f"http://{NVR_IP}:{PORT}{path}",
            auth=auth,
            headers={"Accept": "application/xml"},
            timeout=(3, 8),
            allow_redirects=False,
        )
    except RequestException as exc:
        print(f"Request error: {type(exc).__name__}")
        return None

    print(f"HTTP status: {response.status_code}")

    if response.status_code != 200:
        print("Result: NOT_VERIFIED")
        return None

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        print("Result: INVALID_XML")
        return None

    print(f"XML root: {local_name(root.tag)}")
    return root


def show_hdds(root: ET.Element | None) -> None:
    if root is None:
        print("HDD inventory: NOT_VERIFIED")
        return

    hdds = [
        node for node in root.iter()
        if local_name(node.tag) == "hdd"
        and child_text(node, "status", "capacity", "freeSpace")
    ]

    if not hdds:
        print("No HDD entries recognized in this response.")
        return

    print(f"HDD entries recognized: {len(hdds)}")

    error_count = 0
    ok_count = 0

    for index, hdd in enumerate(hdds, start=1):
        disk_id = child_text(hdd, "id") or "ID_NOT_RETURNED"
        name = child_text(hdd, "hddName") or "NAME_NOT_RETURNED"
        disk_type = child_text(hdd, "hddType") or "TYPE_NOT_RETURNED"
        status = child_text(hdd, "status") or "STATUS_NOT_RETURNED"
        capacity = child_text(hdd, "capacity") or "NOT_RETURNED"
        free_space = child_text(hdd, "freeSpace") or "NOT_RETURNED"
        prop = child_text(hdd, "property") or "NOT_RETURNED"
        group = child_text(hdd, "group") or "NOT_RETURNED"

        normalized_status = status.strip().lower()
        if normalized_status == "error":
            error_count += 1
        elif normalized_status == "ok":
            ok_count += 1

        print(f"\nHDD entry #{index}")
        print(f"  ID: {disk_id}")
        print(f"  Name: {name}")
        print(f"  Type: {disk_type}")
        print(f"  Status reported by NVR: {status}")
        print(f"  Capacity reported (MB): {capacity}")
        print(f"  Free space reported (MB): {free_space}")
        print(f"  Property: {prop}")
        print(f"  Group: {group}")

    print("\nHDD status summary:")
    print(f"  Reported OK: {ok_count}")
    print(f"  Reported error: {error_count}")
    print(
        "  Important: these are raw device results; compare them "
        "with the NVR storage interface."
    )


def show_search_profile(root: ET.Element | None) -> None:
    if root is None:
        print("Search profile: NOT_VERIFIED")
        return

    fields = [
        "maxConcurrentSearches",
        "maxSearchMatchResults",
        "maxSearchTimeSpans",
        "maxSearchTracks",
    ]

    print("Search capabilities returned:")
    for field in fields:
        value = child_text(root, field)
        print(f"  {field}: {value or 'NOT_RETURNED'}")

    profiles = [
        node for node in root.iter()
        if local_name(node.tag) == "searchprofile" and node.text
    ]
    for profile in profiles:
        value = profile.text.strip()
        if value:
            print(f"  searchProfile: {value}")


def show_tracks(root: ET.Element | None) -> None:
    if root is None:
        print("Recording tracks: NOT_VERIFIED")
        return

    tracks = [
        node for node in root.iter()
        if local_name(node.tag) == "track"
        and (
            child_text(node, "id")
            or child_text(node, "srcChannel")
            or child_text(node, "trackGUID")
        )
    ]

    if not tracks:
        print("No track entries recognized in this response.")
        return

    print(f"Track entries recognized: {len(tracks)}")

    for index, track in enumerate(tracks, start=1):
        # Deliberately omit source URLs and login-related fields.
        track_id = child_text(track, "id") or "NOT_RETURNED"
        source_channel = child_text(track, "srcChannel") or "NOT_RETURNED"
        description = child_text(track, "description") or "NOT_RETURNED"
        enabled = child_text(track, "enable") or "NOT_RETURNED"
        default_mode = (
            child_text(track, "defaultRecordingMode") or "NOT_RETURNED"
        )

        print(f"\nTrack entry #{index}")
        print(f"  Track ID: {track_id}")
        print(f"  Source channel: {source_channel}")
        print(f"  Description: {description}")
        print(f"  Enable field: {enabled}")
        print(f"  Default recording mode: {default_mode}")


def show_nvr_time(root: ET.Element | None) -> None:
    if root is None:
        print("NVR time: NOT_VERIFIED")
        return

    for field in ("localTime", "timeMode", "timeZone"):
        value = child_text(root, field)
        print(f"  {field}: {value or 'NOT_RETURNED'}")


def main() -> int:
    global NVR_IP
    NVR_IP = ask_ip()
    username = input("NVR username [admin]: ").strip() or "admin"
    password = getpass("Password (input hidden): ")

    if not password:
        print("Password cannot be empty.")
        return 2

    auth = HTTPDigestAuth(username, password)

    with requests.Session() as session:
        session.trust_env = False

        storage = fetch_xml(
            session, auth, "Storage summary",
            "/ISAPI/ContentMgmt/Storage",
        )
        show_hdds(storage)

        hdd_list = fetch_xml(
            session, auth, "HDD list",
            "/ISAPI/ContentMgmt/Storage/hdd",
        )
        show_hdds(hdd_list)

        profile = fetch_xml(
            session, auth, "Recording search profile",
            "/ISAPI/ContentMgmt/search/profile",
        )
        show_search_profile(profile)

        tracks = fetch_xml(
            session, auth, "Recording tracks",
            "/ISAPI/ContentMgmt/record/tracks",
        )
        show_tracks(tracks)

        nvr_time = fetch_xml(
            session, auth, "NVR time",
            "/ISAPI/System/time",
        )
        show_nvr_time(nvr_time)

    print("\nInspection complete. No device settings were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())