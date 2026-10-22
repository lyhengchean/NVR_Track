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
) -> dict[str, Any]:
    """Calculate overall status without hiding failed or incomplete checks."""

    critical: list[str] = []
    warnings: list[str] = []
    unknown: list[str] = []

    if storage_status == "CRITICAL":
        critical.append("Storage has reported critical HDD errors.")

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
        warnings.append("Storage has a warning that requires investigation.")
    elif storage_status not in {"HEALTHY", "CRITICAL", "WARNING"}:
        unknown.append(f"Storage status not verified: {storage_status}.")

    if recording_status != "RECORDING_EVIDENCE_FOUND":
        unknown.append(
            f"Recording check is not verified: {recording_status}."
        )

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