"""Generate a JSON snapshot for one NVR."""

import argparse
from datetime import datetime, timezone
from getpass import getpass
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import InventoryError, load_inventory
from app.isapi import (
    get_channel_inventory,
    get_channel_statuses,
    get_device_info,
)
from app.reporting import determine_overall_status
from app.storage import get_storage_check


ROOT = Path(__file__).resolve().parent.parent
INVENTORY_PATH = ROOT / "config" / "nvrs.local.json"
TIMEZONE = ZoneInfo("Asia/Phnom_Penh")


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a one-NVR JSON monitoring snapshot."
    )
    parser.add_argument("--ip", default="10.233.220.51")
    parser.add_argument("--port", type=int, default=80)
    args = parser.parse_args()

    started_at = now_local()

    try:
        nvrs = load_inventory(INVENTORY_PATH)
    except InventoryError as exc:
        print(f"Cannot load local NVR inventory: {exc}")
        return 2

    configured_nvr = next(
        (nvr for nvr in nvrs if nvr.ip == args.ip),
        None,
    )

    if configured_nvr is None:
        print("NVR IP is not present in the local inventory.")
        print("Add it to config/nvrs.local.json before generating a report.")
        return 2

    username = input("NVR username [admin]: ").strip() or "admin"
    password = getpass("Password (input hidden): ")

    if not password:
        print("Password cannot be empty.")
        return 2

    print(f"\nChecking NVR {configured_nvr.id} at {args.ip}:{args.port}...")

    device = get_device_info(
        args.ip, username, password, args.port
    )
    inventory = get_channel_inventory(
        args.ip, username, password, args.port
    )
    channel_status_result = get_channel_statuses(
        args.ip, username, password, args.port
    )
    storage = get_storage_check(
        args.ip, username, password, args.port
    )

    inventory_ok = inventory.status == "CHANNELS_RETRIEVED"
    channel_status_ok = (
        channel_status_result.status == "CHANNEL_STATUS_RETRIEVED"
    )

    status_by_id = {
        item.channel_id: item.online
        for item in channel_status_result.channels
    }

    channel_rows = []
    for channel in inventory.channels:
        online = status_by_id.get(channel.channel_id)

        if channel.channel_id not in status_by_id:
            state = "NOT_RETURNED"
        elif online is True:
            state = "ONLINE"
        elif online is False:
            state = "OFFLINE"
        else:
            state = "UNKNOWN"

        channel_rows.append(
            {
                "channel_id": channel.channel_id,
                "name": channel.name,
                "camera_ip": channel.camera_ip,
                "management_port": channel.management_port,
                "reported_status": state,
            }
        )

    online_count = sum(
        item.online is True for item in channel_status_result.channels
    )
    offline_count = sum(
        item.online is False for item in channel_status_result.channels
    )
    unknown_count = sum(
        item.online is None for item in channel_status_result.channels
    )

    actual_count = (
        len(inventory.channels) if inventory_ok else None
    )

    # The prior search returned a segment extending past the NVR clock.
    # Keep this unresolved until a validated recording check is integrated.
    recording_status = "UNKNOWN"

    overall = determine_overall_status(
        device_info_status=device.status,
        inventory_status=inventory.status,
        channel_status=channel_status_result.status,
        storage_status=storage.status,
        offline_count=offline_count,
        expected_count=configured_nvr.expected_channels,
        actual_count=actual_count,
        recording_status=recording_status,
    )

    completed_at = now_local()

    report = {
        "schema_version": 1,
        "nvr": {
            "id": configured_nvr.id,
            "ip": args.ip,
            "expected_channels": configured_nvr.expected_channels,
        },
        "check_started_at": started_at.isoformat(),
        "check_completed_at": completed_at.isoformat(),
        "check_completed_at_utc": (
            completed_at.astimezone(timezone.utc).isoformat()
        ),
        "device_info": {
            "status": device.status,
            "http_status": device.http_status,
            "model": device.model,
            "firmware_version": device.firmware_version,
            "message": device.message,
        },
        "channel_inventory": {
            "status": inventory.status,
            "http_status": inventory.http_status,
            "configured_count": actual_count,
            "expected_count": configured_nvr.expected_channels,
            "count_matches": (
                actual_count == configured_nvr.expected_channels
                if actual_count is not None
                else None
            ),
            "channels": channel_rows,
            "message": inventory.message,
        },
        "channel_status": {
            "status": channel_status_result.status,
            "http_status": channel_status_result.http_status,
            "online_count": online_count,
            "offline_count": offline_count,
            "unknown_count": unknown_count,
            "message": channel_status_result.message,
        },
        "storage": {
            "status": storage.status,
            "http_status": storage.http_status,
            "message": storage.message,
            "disks": [
                {
                    "id": disk.id,
                    "name": disk.name,
                    "type": disk.disk_type,
                    "reported_status": disk.status,
                    "capacity_mb": disk.capacity_mb,
                    "free_space_mb": disk.free_space_mb,
                    "property": disk.property,
                    "group": disk.group,
                }
                for disk in storage.disks
            ],
        },
        "recording": {
            "status": recording_status,
            "message": (
                "Recording-search segment end exceeded the NVR clock. "
                "Playback-time verification is required."
            ),
        },
        "overall": overall,
    }

    report_dir = ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    timestamp = completed_at.strftime("%Y%m%dT%H%M%S%z")
    output_path = report_dir / (
        f"{configured_nvr.id}_{timestamp}.json"
    )

    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\n========== NVR REPORT ==========")
    print(f"Overall status: {overall['status']}")
    print(f"Device information: {device.status}")
    print(f"Channel inventory: {inventory.status}")
    print(f"Channel status: {channel_status_result.status}")
    print(f"Storage: {storage.status}")
    print(f"Online channels: {online_count}")
    print(f"Offline channels: {offline_count}")
    print(f"Unknown channel statuses: {unknown_count}")
    print(f"Recording: {recording_status}")
    print(f"JSON report saved: {output_path}")
    print("Credentials were not written to the report.")

    return 0 if overall["status"] == "HEALTHY" else 1


if __name__ == "__main__":
    raise SystemExit(main())