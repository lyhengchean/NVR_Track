"""Conservative overall status classification for NVR monitoring."""

from typing import Any


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
) -> dict[str, Any]:
    critical: list[str] = []
    warnings: list[str] = []
    unknown: list[str] = []

    if storage_status == "CRITICAL":
        critical.append("Storage needs urgent review: abnormal disks, expected disks missing, or no operational storage.")

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
        warnings.append(f"{offline_count} channels are reported offline.")

    if (
        expected_count is not None
        and actual_count is not None
        and actual_count != expected_count
    ):
        warnings.append(
            f"Configured channel count {actual_count} differs "
            f"from expected count {expected_count}."
        )

    if storage_status == "WARNING":
        warnings.append("Storage warning: review disk detection, free space, and overwrite/quota policy.")
    elif storage_status not in {"HEALTHY", "CRITICAL", "WARNING"}:
        unknown.append(f"Storage status not verified: {storage_status}.")

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
    }