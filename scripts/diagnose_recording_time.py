from datetime import timedelta
from getpass import getpass
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth

from app.recording import (
    child_text,
    make_search_xml,
    parse_timestamp,
)

IP = "10.233.220.51"
PORT = 80
TRACK_ID = 201

username = input("NVR username [admin]: ").strip() or "admin"
password = getpass("Password (input hidden): ")

base_url = f"http://{IP}:{PORT}"
auth = HTTPDigestAuth(username, password)

with requests.Session() as session:
    session.trust_env = False

    clock = session.get(
        f"{base_url}/ISAPI/System/time",
        auth=auth,
        headers={"Accept": "application/xml"},
        timeout=(3, 8),
        allow_redirects=False,
    )

    print("\nClock HTTP status:", clock.status_code)
    print("Raw NVR clock XML:")
    print(clock.text)

    if clock.status_code != 200:
        raise SystemExit("Could not retrieve the NVR clock.")

    clock_root = ET.fromstring(clock.content)
    raw_time = child_text(clock_root, "localTime")
    nvr_now = parse_timestamp(raw_time)
    start_utc = nvr_now - timedelta(minutes=60)

    print("\nParsed NVR clock (UTC):", nvr_now.isoformat())
    print("Requested start (UTC):", start_utc.isoformat())
    print("Requested end (UTC):", nvr_now.isoformat())

    payload = make_search_xml(
        TRACK_ID,
        start_utc,
        nvr_now,
        10,
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

    print("\nRecording search HTTP status:", response.status_code)
    print("Raw recording-search XML:")
    print(response.text)