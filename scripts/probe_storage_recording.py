"""Read-only discovery of NVR storage and recording-search endpoints."""

from getpass import getpass
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException


NVR_IP = "10.233.220.51"
PORT = 80

ENDPOINTS = [
    ("Storage summary", "/ISAPI/ContentMgmt/Storage"),
    ("HDD list", "/ISAPI/ContentMgmt/Storage/hdd"),
    ("Recording search profile", "/ISAPI/ContentMgmt/search/profile"),
    ("Recording tracks", "/ISAPI/ContentMgmt/record/tracks"),
    ("NVR time", "/ISAPI/System/time"),
]

SAFE_VALUE_TAGS = {
    "status",
    "statuscode",
    "statusstring",
    "substatuscode",
    "capacity",
    "freespace",
    "property",
    "healthstate",
    "totalcapacity",
    "usedspace",
    "workmode",
    "maxsearchresults",
    "maxsearchnum",
    "maxconcurrentsearch",
    "maxconcurrence",
    "trackid",
    "diskid",
    "hddid",
}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def summarize_xml(body: bytes) -> None:
    """Print XML structure and safe diagnostic values, never the raw body."""

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        print("XML parsing: FAILED or response is not XML")
        return

    print(f"XML root: {local_name(root.tag)}")

    tag_names = sorted({local_name(node.tag) for node in root.iter()})
    print("Element names: " + ", ".join(tag_names[:50]))

    shown = 0
    for node in root.iter():
        tag = local_name(node.tag)
        if tag not in SAFE_VALUE_TAGS or not node.text:
            continue

        value = node.text.strip()
        if not value:
            continue

        # Do not display identifiers that are not simple numeric IDs.
        if tag in {"trackid", "diskid", "hddid"} and not value.isdigit():
            continue

        print(f"  {tag}: {value}")
        shown += 1

        if shown >= 40:
            print("  ... additional safe values omitted")
            break


def main() -> int:
    username = input("NVR username [admin]: ").strip() or "admin"
    password = getpass("Password (input hidden): ")

    if not password:
        print("Password cannot be empty.")
        return 2

    with requests.Session() as session:
        session.trust_env = False

        for label, path in ENDPOINTS:
            print(f"\n========== {label} ==========")
            print(f"GET {path}")

            try:
                response = session.get(
                    f"http://{NVR_IP}:{PORT}{path}",
                    auth=HTTPDigestAuth(username, password),
                    headers={"Accept": "application/xml"},
                    timeout=(3, 8),
                    allow_redirects=False,
                )
            except RequestException as exc:
                # Report the exception type only, not its full message.
                print(f"Request failed: {type(exc).__name__}")
                continue

            print(f"HTTP status: {response.status_code}")
            print(
                "Content type: "
                f"{response.headers.get('Content-Type', 'NOT_RETURNED')}"
            )

            if response.status_code != 200:
                print("Endpoint result: NOT_CONFIRMED")
                continue

            summarize_xml(response.content)

    print("\nProbe completed. No device configuration was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
