"""Manually test read-only ISAPI access to one authorized NVR."""

import argparse
from getpass import getpass
import sys

from nvr_track.nvr.isapi import get_device_info


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test read-only Hikvision device-information access."
    )
    parser.add_argument(
        "--ip",
        required=True,
        help="NVR IPv4 address",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=80,
        help="NVR HTTP port (default: 80)",
    )
    args = parser.parse_args()

    print(f"NVR address: {args.ip}:{args.port}")
    print("Endpoint: /ISAPI/System/deviceInfo")

    username = input("NVR username [admin]: ").strip() or "admin"
    password = getpass("Password (input hidden): ")

    result = get_device_info(
        ip=args.ip,
        username=username,
        password=password,
        port=args.port,
    )

    print()
    print(f"Status: {result.status}")

    if result.http_status is not None:
        print(f"HTTP status: {result.http_status}")

    print(f"Message: {result.message}")

    if result.status == "DEVICE_INFO_RETRIEVED":
        print(f"Model: {result.model or 'NOT_RETURNED'}")
        print(
            "Firmware version: "
            f"{result.firmware_version or 'NOT_RETURNED'}"
        )
        print("ISAPI device-information check: PASS")
        print("Overall NVR health: NOT_CHECKED")
        return 0

    print("ISAPI device-information check: NOT_CONFIRMED")
    print("Overall NVR health: UNKNOWN")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
