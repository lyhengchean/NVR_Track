"""Conservative overall status classification for NVR monitoring."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nvr_track.monitoring.duplicates import info as duplicate_info, warnings as duplicate_warnings
from nvr_track.monitoring.flapping import warning as flap_warning

LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")


def _local(value: object) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "an unknown time"


STORAGE_CRITICAL = "Storage needs urgent review"
STORAGE_WARNING = "Storage warning"
STORAGE_UNKNOWN = "Storage status not verified"
OFFLINE_SUFFIX = " channels are reported offline."


def storage_findings(storage_status: str, storage_message: str = "") -> tuple[list[str], list[str], list[str]]:
    """(critical, warnings, unknown) lines for the storage check; also used by nvr_track.monitoring.live."""
    # The storage check's own finding (e.g. "HDD 8 (error)"), so alerts and the
    # email say which disk instead of a generic sentence.
    detail = " ".join(str(storage_message or "").split())[:200]
    if storage_status == "CRITICAL":
        return ([f"{STORAGE_CRITICAL}: {detail}" if detail else
                 f"{STORAGE_CRITICAL}: abnormal disks, expected disks missing, or no operational storage."], [], [])
    if storage_status == "WARNING":
        return ([], [f"{STORAGE_WARNING}: {detail}" if detail else
                     f"{STORAGE_WARNING}: review disk detection, free space, and overwrite/quota policy."], [])
    if storage_status != "HEALTHY":
        return [], [], [f"{STORAGE_UNKNOWN}: {storage_status}."]
    return [], [], []


def offline_finding(offline_count: int) -> str:
    return f"{offline_count}{OFFLINE_SUFFIX}"


def determine_overall_status(
    *,
    device_info_status: str,
    inventory_status: str,
    channel_status: str,
    storage_status: str,
    offline_count: int,
    expected_count: int | None,
    actual_count: int | None,
    recording_status: str,
    per_camera_summary: dict | None = None,
    smart: dict | None = None,
    picture_problems: int = 0,
    restart: dict | None = None,
    inventory_changes: dict | None = None,
    storage_message: str = "",
    duplicates: dict | None = None,
    flapping: dict | None = None,
) -> dict[str, Any]:
    critical: list[str] = []
    warnings: list[str] = []
    unknown: list[str] = []
    storage_critical, storage_warnings, storage_unknown = storage_findings(storage_status, storage_message)
    critical += storage_critical

    if device_info_status in {
        "AUTHENTICATION_FAILED",
        "CONNECTION_ERROR",
        "TIMEOUT",
        "HTTP_ERROR",
    }:
        critical.append(
            f"Device-information check failed: {device_info_status}."
        )
    elif device_info_status != "DEVICE_INFO_RETRIEVED":
        unknown.append(
            f"Device information not verified: {device_info_status}."
        )

    if inventory_status != "CHANNELS_RETRIEVED":
        unknown.append(
            f"Channel inventory not verified: {inventory_status}."
        )

    if channel_status != "CHANNEL_STATUS_RETRIEVED":
        unknown.append(
            f"Channel status not verified: {channel_status}."
        )

    if offline_count > 0:
        warnings.append(offline_finding(offline_count))

    if (
        expected_count is not None
        and actual_count is not None
        and actual_count != expected_count
    ):
        warnings.append(
            f"Configured channel count {actual_count} differs "
            f"from expected count {expected_count}."
        )

    warnings += storage_warnings
    unknown += storage_unknown

    # The old NVR-wide probe verifies just one track. When independent camera
    # checks are enabled, it must not override their more precise assessment.
    if not (per_camera_summary and per_camera_summary.get("enabled")):
        if recording_status == "RECORDING_EVIDENCE_FOUND":
            pass
        elif recording_status == "RECORDING_EVIDENCE_FOUND_WITH_TIME_ANOMALY":
            warnings.append(
                "Recording evidence was found, but segment time boundaries need verification."
            )
        elif recording_status == "NO_MATCHES_IN_WINDOW":
            warnings.append(
                "No recording matches were returned for the tested window; verify the schedule."
            )
        else:
            unknown.append(
                f"Recording check is not verified: {recording_status}."
            )

    if per_camera_summary and per_camera_summary.get("enabled"):
        missing = per_camera_summary.get("missing_expected", 0)
        online_missing = per_camera_summary.get("online_missing_expected", 0)
        errors = per_camera_summary.get("search_errors", 0)
        ambiguous = (per_camera_summary.get("inconclusive", 0)
                     + per_camera_summary.get("track_mapping_problems", 0)
                     + per_camera_summary.get("not_checked", 0))
        if missing:
            warnings.append(f"{missing} continuous-recording cameras have no evidence in the configured window.")
        if online_missing:
            warnings.append(f"{online_missing} ONLINE cameras are missing expected continuous recording evidence.")
        if errors:
            unknown.append(f"{errors} per-camera recording searches failed.")
        if ambiguous:
            unknown.append(f"{ambiguous} per-camera recording checks were unverified or not run.")

    smart = smart or {}
    if smart.get("status") == "CRITICAL":
        critical.append(f"Disk SMART failing: {smart.get('message', '')}")
    elif smart.get("status") == "WARNING":
        warnings.append(f"Disk SMART warning: {smart.get('message', '')}")
    if picture_problems:
        warnings.append(f"{picture_problems} cameras have a picture problem "
                        "(black, covered, frozen or moved).")
    if restart and restart.get("detected"):
        warnings.append(f"NVR restarted at about {_local(restart.get('boot_time'))}.")
    changes = (inventory_changes or {}).get("changes") or []
    if changes:
        kinds = {"added": "added", "removed": "removed", "renamed": "renamed", "ip_changed": "IP changed"}
        parts = [f"{sum(c.get('type') == k for c in changes)} {label}" for k, label in kinds.items()
                 if any(c.get("type") == k for c in changes)]
        warnings.append("Camera list changed since the previous report: " + ", ".join(parts) + ".")
    warnings += duplicate_warnings(duplicates)
    unstable = flap_warning(flapping)
    if unstable:
        warnings.append(unstable)

    if critical:
        overall = "CRITICAL"
    elif warnings:
        overall = "WARNING"
    elif unknown:
        overall = "UNKNOWN"
    else:
        overall = "HEALTHY"

    return {
        "status": overall,
        "critical_reasons": critical,
        "warnings": warnings,
        "unknown_checks": unknown,
        # Information only; never changes the status.
        "info": duplicate_info(duplicates),
    }