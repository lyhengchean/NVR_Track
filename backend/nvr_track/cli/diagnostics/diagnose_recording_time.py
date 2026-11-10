"""Show one NVR's clock and the raw reply of a 60-minute recording search (read-only).

python -m nvr_track.cli.diagnostics.diagnose_recording_time --ip <NVR IP> [--track-id 201]
"""
import argparse
from datetime import timedelta
from getpass import getpass
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth

from nvr_track.nvr.recording import (
    child_text,
    make_search_xml,
    parse_timestamp,
)

PORT = 80


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ip", required=True, help="NVR IPv4 address")
    parser.add_argument("--http-port", type=int, default=PORT)
    parser.add_argument("--track-id", type=int, default=201, help="Recording track, e.g. 201 for channel 2")
    args = parser.parse_args()

    username = input("NVR username [admin]: ").strip() or "admin"
    password = getpass("Password (input hidden): ")

    base_url = f"http://{args.ip}:{args.http_port}"
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
            args.track_id,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
