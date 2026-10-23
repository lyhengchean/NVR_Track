"""Generate a JSON snapshot for one NVR."""
import os
import sys
import argparse
from datetime import datetime, timezone
import json
from getpass import getpass
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import InventoryError, load_inventory
from app.isapi import (
    get_channel_inventory,
    get_channel_statuses,
    get_device_info,
)
from app.recording import check_recording_evidence
from app.reporting import determine_overall_status
from app.storage import get_storage_check


ROOT = Path(__file__).resolve().parent.parent
INVENTORY_PATH = ROOT / "config" / "nvrs.local.json"
TIMEZONE = ZoneInfo("Asia/Phnom_Penh")


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate one NVR's JSON monitoring snapshot."
    )
    parser.add_argument(
        "--ip",
        default=None,
        help="NVR IP address. Defaults to the first inventory entry.",
    )
    parser.add_argument(
        "--http-port",
        "--port",
        dest="http_port",
        type=int,
        default=None,
        help="Override the configured NVR HTTP port.",
    )
    parser.add_argument(
        "--recording-track-id",
        type=int,
        default=None,
        help="Verified recording track to test, e.g. 201 for NVR nvr-01.",
    )
    args = parser.parse_args()

    started_at = now_local()

    try:
        nvrs = load_inventory(INVENTORY_PATH)
    except InventoryError as exc:
        print(f"Cannot load local NVR inventory: {exc}")
        return 2

    if args.ip is None:
        configured_nvr = nvrs[0]
    else:
        configured_nvr = next(
            (nvr for nvr in nvrs if nvr.ip == args.ip),
            None,
        )

    if configured_nvr is None:
        print("NVR IP is not present in the local inventory.")
        return 2

    http_port = (
        args.http_port
        if args.http_port is not None
        else configured_nvr.http_port
    )

    recording_track_id = (
        args.recording_track_id
        if args.recording_track_id is not None
        else configured_nvr.recording_track_id
    )
    
    # Select credentials for this specific NVR.
    # Example: nvr-01 -> NVR_01_USERNAME / NVR_01_PASSWORD
    env_suffix = (
        configured_nvr.id.upper()
        .removeprefix("NVR-")
        .replace("-", "_")
    )

    username_key = f"NVR_{env_suffix}_USERNAME"
    password_key = f"NVR_{env_suffix}_PASSWORD"

    # Prefer per-NVR credentials; fall back to shared credentials.
    username = (
        os.environ.get(username_key, "").strip()
        or os.environ.get("NVR_USERNAME", "").strip()
    )
    password = (
        os.environ.get(password_key, "")
        or os.environ.get("NVR_PASSWORD", "")
    )

    if not username or not password:
        if not sys.stdin.isatty():
            print(
                f"Missing credentials for {configured_nvr.id}. "
                f"Configure {username_key} and {password_key} "
                "or shared credentials in .env."
            )
            return 2

        if not username:
            username = input("NVR username [admin]: ").strip() or "admin"

        if not password:
            password = getpass("Password (input hidden): ")

    if not password:
        print("Password cannot be empty.")
        return 2

    print(
        f"\nChecking NVR {configured_nvr.id} "
        f"at {configured_nvr.ip}:{http_port}..."
    )

    device = get_device_info(
        configured_nvr.ip, username, password, http_port
    )
    inventory = get_channel_inventory(
        configured_nvr.ip, username, password, http_port
    )
    channel_status_result = get_channel_statuses(
        configured_nvr.ip, username, password, http_port
    )
    storage = get_storage_check(
        configured_nvr.ip, username, password, http_port
    )

    if recording_track_id is not None:
        recording = check_recording_evidence(
            ip=configured_nvr.ip,
            username=username,
            password=password,
            port=http_port,
            track_id=recording_track_id,
            minutes=60,
            max_results=10,
        )
    else:
        recording = {
            "status": "NOT_CHECKED_TRACK_ID_NOT_CONFIGURED",
            "message": (
                "No verified recording track ID was supplied. "
                "Recording search was not performed."
            ),
            "http_status": None,
            "track_id": None,
            "requested_window_utc": {"start": None, "end": None},
            "num_matches": None,
            "total_matches": None,
            "matches": [],
            "anomalies": [],
        }

    inventory_ok = inventory.status == "CHANNELS_RETRIEVED"

    status_ok = (
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

        channel_rows.append({
            "channel_id": channel.channel_id,
            "name": channel.name,
            "camera_ip": channel.camera_ip,
            "management_port": channel.management_port,
            "reported_status": state,
        })

    online_count = sum(
        item.online is True for item in channel_status_result.channels
    )
    offline_count = sum(
        item.online is False for item in channel_status_result.channels
    )
    unknown_count = sum(
        item.online is None for item in channel_status_result.channels
    )

    actual_count = len(inventory.channels) if inventory_ok else None

    overall = determine_overall_status(
        device_info_status=device.status,
        inventory_status=inventory.status,
        channel_status=channel_status_result.status,
        storage_status=storage.status,
        offline_count=offline_count,
        expected_count=configured_nvr.expected_channels,
        actual_count=actual_count,
        recording_status=recording["status"],
    )

    completed_at = now_local()

    report = {
        "schema_version": 1,
        "nvr": {
            "id": configured_nvr.id,
            "ip": configured_nvr.ip,
            "http_port": http_port,
            "server_port": configured_nvr.server_port,
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
        "recording": recording,
        "overall": overall,
    }

    report_dir = ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    timestamp = completed_at.strftime("%Y%m%dT%H%M%S%f%z")
    output_path = report_dir / f"{configured_nvr.id}_{timestamp}.json"

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
    print(f"Recording: {recording['status']}")
    print(f"Online channels: {online_count}")
    print(f"Offline channels: {offline_count}")
    print(f"Unknown channel statuses: {unknown_count}")
    print(f"JSON report saved: {output_path}")
    print("Credentials were not written to the report.")

    return 0 if overall["status"] == "HEALTHY" else 1


if __name__ == "__main__":
    raise SystemExit(main())