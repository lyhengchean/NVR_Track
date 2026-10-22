"""Read-only diagnostic for one NVR's digital channel inventory and status."""

import argparse
from getpass import getpass
from pathlib import Path

from app.config import InventoryError, load_inventory
from app.isapi import get_channel_inventory, get_channel_statuses


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_INVENTORY = PROJECT_ROOT / "config" / "nvrs.local.json"


def expected_channel_count(ip: str) -> int | None:
    """Return the expected count for an IP in the local inventory, if present."""
    try:
        nvrs = load_inventory(LOCAL_INVENTORY)
    except InventoryError:
        return None

    for nvr in nvrs:
        if nvr.ip == ip:
            return nvr.expected_channels

    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Hikvision digital-channel diagnostic."
    )
    parser.add_argument(
        "--ip",
        default="10.233.220.51",
        help="NVR IPv4 address (default: 10.233.220.51)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=80,
        help="NVR HTTP port (default: 80)",
    )
    args = parser.parse_args()

    print(f"NVR: {args.ip}:{args.port}")
    username = input("NVR username [admin]: ").strip() or "admin"
    password = getpass("Password (input hidden): ")

    print("\n========== CHANNEL INVENTORY ==========")
    inventory = get_channel_inventory(
        args.ip, username, password, args.port
    )

    print(f"Status: {inventory.status}")
    if inventory.http_status is not None:
        print(f"HTTP status: {inventory.http_status}")
    print(f"Message: {inventory.message}")

    expected = expected_channel_count(args.ip)
    if expected is None:
        print("Expected channel count: NOT_FOUND_IN_LOCAL_INVENTORY")
    else:
        print(f"Expected channel count: {expected}")

    inventory_ok = inventory.status == "CHANNELS_RETRIEVED"

    if inventory_ok:
        actual = len(inventory.channels)
        print(f"Configured digital channels returned: {actual}")

        if expected is not None:
            if actual == expected:
                print("Channel count comparison: MATCH")
            else:
                print("Channel count comparison: MISMATCH")
                print("This requires reconciliation with the NVR interface.")

        print("\nChannel entries:")
        for channel in sorted(
            inventory.channels,
            key=lambda item: (
                not item.channel_id.isdigit(),
                int(item.channel_id)
                if item.channel_id.isdigit()
                else item.channel_id,
            ),
        ):
            print(
                f"  ID={channel.channel_id}"
                f" | Name={channel.name or 'NOT_RETURNED'}"
                f" | CameraIP={channel.camera_ip or 'NOT_RETURNED'}"
                f" | ManagementPort={channel.management_port or 'NOT_RETURNED'}"
            )
    else:
        print("Channel inventory: NOT_VERIFIED")

    print("\n========== CHANNEL STATUS ==========")
    status_result = get_channel_statuses(
        args.ip, username, password, args.port
    )

    print(f"Status: {status_result.status}")
    if status_result.http_status is not None:
        print(f"HTTP status: {status_result.http_status}")
    print(f"Message: {status_result.message}")

    statuses_ok = status_result.status == "CHANNEL_STATUS_RETRIEVED"

    if status_result.channels:
        online_ids = sorted(
            (item.channel_id for item in status_result.channels
             if item.online is True),
            key=lambda value: (not value.isdigit(),
                               int(value) if value.isdigit() else value),
        )
        offline_ids = sorted(
            (item.channel_id for item in status_result.channels
             if item.online is False),
            key=lambda value: (not value.isdigit(),
                               int(value) if value.isdigit() else value),
        )
        unknown_ids = sorted(
            (item.channel_id for item in status_result.channels
             if item.online is None),
            key=lambda value: (not value.isdigit(),
                               int(value) if value.isdigit() else value),
        )

        print(f"Status entries returned: {len(status_result.channels)}")
        print(f"Online: {len(online_ids)}")
        print(f"Offline: {len(offline_ids)}")
        print(f"Unknown status: {len(unknown_ids)}")

        if offline_ids:
            print("Offline channel IDs: " + ", ".join(offline_ids))
        if unknown_ids:
            print("Unknown channel IDs: " + ", ".join(unknown_ids))

        if inventory_ok:
            inventory_ids = {
                item.channel_id for item in inventory.channels
            }
            status_ids = {
                item.channel_id for item in status_result.channels
            }
            missing = sorted(inventory_ids - status_ids)
            extra = sorted(status_ids - inventory_ids)

            print(f"Configured channels without status entries: {len(missing)}")
            if missing:
                print("Missing status IDs: " + ", ".join(missing))

            print(f"Status entries absent from inventory: {len(extra)}")
            if extra:
                print("Extra status IDs: " + ", ".join(extra))
    else:
        print("Channel online/offline status: NOT_VERIFIED")

    print("\n========== SUMMARY ==========")
    print(f"Inventory check: {'PASS' if inventory_ok else 'NOT_CONFIRMED'}")
    print(f"Status check: {'PASS' if statuses_ok else 'NOT_CONFIRMED'}")
    print("Overall NVR health: NOT_CHECKED")
    print("Recording health: NOT_CHECKED")
    print("HDD health: NOT_CHECKED")

    return 0 if inventory_ok and statuses_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())