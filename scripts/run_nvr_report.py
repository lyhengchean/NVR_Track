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
from app.camera_recording import check_all_cameras
from app.reporting import determine_overall_status
from app.report_lock import acquire_report_lock
from app.storage import get_storage_check


ROOT = Path(__file__).resolve().parent.parent
INVENTORY_PATH = ROOT / "config" / "nvrs.local.json"
TIMEZONE = ZoneInfo("Asia/Phnom_Penh")


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def read_previous_camera_field(report_dir: Path, nvr_id: str, field: str) -> dict:
    """Reuse results from the newest saved report without touching old snapshots."""
    candidates = sorted(report_dir.glob(f"{nvr_id}_*.json"), reverse=True)
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8") as handle:
                report = json.load(handle)
            if report.get("nvr", {}).get("id") != nvr_id:
                continue
            return {
                str(ch.get("channel_id")): ch.get(field)
                for ch in report.get("channel_inventory", {}).get("channels", [])
                if isinstance(ch, dict)
            }
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return {}


def read_previous_history(report_dir: Path, nvr_id: str) -> dict:
    return read_previous_camera_field(report_dir, nvr_id, "historical_recording_result")


def read_previous_retention(report_dir: Path, nvr_id: str) -> dict:
    return read_previous_camera_field(report_dir, nvr_id, "retention_result")


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

    # Lock report generation for this NVR across Compose invocations.
    # The shared reports volume makes this effective across reporter containers.
    report_dir = ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = acquire_report_lock(report_dir, configured_nvr.id)
    if lock_handle is False:
        print(f"Another report is already running for {configured_nvr.id}; no queries sent.")
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
        configured_nvr.ip, username, password, http_port,
        expected_sata_hdd_ids=configured_nvr.expected_sata_hdd_ids,
    )

    def integer_setting(name: str, default: int, lower: int, upper: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
            if lower <= value <= upper:
                return value
        except ValueError:
            pass
        print(f"Invalid {name}; using safe default {default}.")
        return default

    window_minutes = integer_setting("NVR_RECORDING_WINDOW_MINUTES", 60, 1, 1440)
    max_results = integer_setting("NVR_RECORDING_MAX_RESULTS", 10, 1, 64)
    time_budget = integer_setting("NVR_RECORDING_MAX_NVR_SECONDS", 300, 15, 3600)
    if recording_track_id is not None:
        recording = check_recording_evidence(
            ip=configured_nvr.ip,
            username=username,
            password=password,
            port=http_port,
            track_id=recording_track_id,
            minutes=window_minutes,
            max_results=max_results,
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
            "recording_expectation": configured_nvr.recording_expectations.get(
                str(channel.channel_id), "unspecified"
            ),
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

    allowed = {
        item.strip().lower()
        for item in os.environ.get("NVR_PER_CAMERA_RECORDING_IDS", "").split(",")
        if item.strip()
    }
    retention_ids = {
        item.strip().lower()
        for item in os.environ.get("NVR_RETENTION_NVR_IDS", "").split(",")
        if item.strip()
    }
    retention_enabled = (os.environ.get("NVR_RETENTION_ENABLED", "false").lower() == "true"
                         and configured_nvr.id.lower() in retention_ids)
    per_camera_summary = check_all_cameras(
        ip=configured_nvr.ip, port=http_port, username=username, password=password,
        channels=channel_rows, enabled=configured_nvr.id.lower() in allowed,
        minutes=window_minutes, max_results=max_results,
        budget_seconds=time_budget,
        historical_enabled=os.environ.get("NVR_HISTORICAL_ENABLED", "true").lower() == "true",
        historical_days=integer_setting("NVR_HISTORICAL_LOOKBACK_DAYS", 7, 1, 7),
        historical_max_cameras=integer_setting("NVR_HISTORICAL_MAX_CAMERAS_PER_RUN", 2, 0, 32),
        historical_ttl_hours=integer_setting("NVR_HISTORICAL_CACHE_HOURS", 12, 1, 72),
        previous_history=read_previous_history(report_dir, configured_nvr.id),
        retention_enabled=retention_enabled,
        retention_days=integer_setting("NVR_RETENTION_WINDOW_DAYS", 30, 1, 90),
        retention_max_cameras=integer_setting("NVR_RETENTION_MAX_CAMERAS_PER_RUN", 2, 0, 32),
        retention_ttl_hours=integer_setting("NVR_RETENTION_CACHE_HOURS", 168, 1, 720),
        previous_retention=read_previous_retention(report_dir, configured_nvr.id),
        retry_delay_seconds=integer_setting("NVR_RECORDING_RETRY_DELAY_SECONDS", 15, 0, 60),
    )

    overall = determine_overall_status(
        device_info_status=device.status,
        inventory_status=inventory.status,
        channel_status=channel_status_result.status,
        storage_status=storage.status,
        offline_count=offline_count,
        expected_count=configured_nvr.expected_channels,
        actual_count=actual_count,
        recording_status=recording["status"],
        per_camera_summary=per_camera_summary,
    )

    completed_at = now_local()

    report = {
        "schema_version": 2,
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
        "per_camera_recording": per_camera_summary,
        "overall": overall,
    }

    timestamp = completed_at.strftime("%Y%m%dT%H%M%S%f%z")
    output_path = report_dir / f"{configured_nvr.id}_{timestamp}.json"

    # Write to a hidden temporary file, then rename, so the dashboard and the
    # next run never read a half-written report.
    temporary_path = report_dir / f".{output_path.name}.tmp"
    temporary_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary_path, output_path)

    print("\n========== NVR REPORT ==========")
    print(f"Overall status: {overall['status']}")
    print(f"Device information: {device.status}")
    print(f"Channel inventory: {inventory.status}")
    print(f"Channel status: {channel_status_result.status}")
    print(f"Storage: {storage.status}")
    print(f"NVR-wide recording: {recording['status']}")
    print(f"Per-camera recording: {per_camera_summary['evidence']} evidence; "
          f"{per_camera_summary['no_recent_evidence']} no recent evidence; "
          f"{per_camera_summary['not_checked']} not checked")
    retention_info = per_camera_summary.get("retention", {})
    print(f"Recording calendar: {retention_info.get('verified', 0)} verified; "
          f"{retention_info.get('partial', 0)} partial; "
          f"{retention_info.get('pending', 0)} pending; "
          f"{retention_info.get('unsupported', 0)} unsupported")
    print(f"Online channels: {online_count}")
    print(f"Offline channels: {offline_count}")
    print(f"Unknown channel statuses: {unknown_count}")
    print(f"JSON report saved: {output_path}")
    print("Credentials were not written to the report.")

    if lock_handle:
        lock_handle.close()
    return 0 if overall["status"] == "HEALTHY" else 1


if __name__ == "__main__":
    raise SystemExit(main())