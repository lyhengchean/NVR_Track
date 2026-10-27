"""Load and validate the NVR inventory without contacting devices."""

import json
import re
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path


class InventoryError(ValueError):
    """Raised when the NVR inventory is invalid."""


@dataclass(frozen=True)
class NVRConfig:
    id: str
    ip: str
    expected_channels: int
    http_port: int = 80
    server_port: int | None = None
    recording_track_id: int | None = None
    recording_expectations: dict[str, str] = field(default_factory=dict)
    expected_sata_hdd_ids: tuple[int, ...] = ()


def load_inventory(path: str | Path) -> list[NVRConfig]:
    """Load an inventory JSON file and validate its contents."""

    inventory_path = Path(path)

    try:
        with inventory_path.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
    except FileNotFoundError as exc:
        raise InventoryError(
            f"Inventory file does not exist: {inventory_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise InventoryError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except OSError as exc:
        raise InventoryError(
            f"Cannot read inventory file: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise InventoryError("Inventory root must be a JSON object.")

    if payload.get("schema_version") != 1:
        raise InventoryError("Unsupported or missing schema_version.")

    entries = payload.get("nvrs")
    if not isinstance(entries, list) or not entries:
        raise InventoryError("'nvrs' must be a non-empty list.")

    result: list[NVRConfig] = []
    seen_ids: set[str] = set()
    seen_ips: set[str] = set()

    for position, entry in enumerate(entries, start=1):
        label = f"nvrs[{position - 1}]"

        if not isinstance(entry, dict):
            raise InventoryError(f"{label} must be an object.")

        device_id = entry.get("id")
        if (
            not isinstance(device_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", device_id)
        ):
            raise InventoryError(f"{label}: invalid device ID.")

        if device_id in seen_ids:
            raise InventoryError(f"{label}: duplicate device ID '{device_id}'.")

        raw_ip = entry.get("ip")
        if not isinstance(raw_ip, str):
            raise InventoryError(f"{label}: IP address must be a string.")

        try:
            parsed_ip = ip_address(raw_ip)
        except ValueError as exc:
            raise InventoryError(f"{label}: invalid IP address.") from exc

        if parsed_ip.version != 4:
            raise InventoryError(f"{label}: only IPv4 is supported currently.")

        normalized_ip = str(parsed_ip)
        if normalized_ip in seen_ips:
            raise InventoryError(f"{label}: duplicate IP address.")

        channel_count = entry.get("expected_channels")
        if (
            isinstance(channel_count, bool)
            or not isinstance(channel_count, int)
            or not 1 <= channel_count <= 512
        ):
            raise InventoryError(
                f"{label}: expected_channels must be an integer from 1 to 512."
            )
        
        http_port = entry.get("http_port", 80)
        if (
            isinstance(http_port, bool)
            or not isinstance(http_port, int)
            or not 1 <= http_port <= 65535
        ):
            raise InventoryError(
                f"{label}: http_port must be an integer from 1 to 65535."
            )

        server_port = entry.get("server_port")
        if server_port is not None and (
            isinstance(server_port, bool)
            or not isinstance(server_port, int)
            or not 1 <= server_port <= 65535
        ):
            raise InventoryError(
                f"{label}: server_port must be an integer from 1 to 65535."
            )

        recording_track_id = entry.get("recording_track_id")
        if recording_track_id is not None and (
            isinstance(recording_track_id, bool)
            or not isinstance(recording_track_id, int)
            or recording_track_id <= 0
        ):
            raise InventoryError(
                f"{label}: recording_track_id must be a positive integer."
            )

        expectations = entry.get("recording_expectations", {})
        if not isinstance(expectations, dict) or any(
            not isinstance(k, str) or not k.isdecimal() or int(k) <= 0
            or not isinstance(v, str) or v not in {"continuous", "event", "unspecified"}
            for k, v in expectations.items()
        ):
            raise InventoryError(
                f"{label}: recording_expectations must map channel IDs to continuous, event or unspecified."
            )

        # Only list SATA disks independently confirmed as physically installed.
        # Empty/unconfirmed slots must not be guessed from the NVR's HDD list.
        raw_expected_hdds = entry.get("expected_sata_hdd_ids", [])
        if (
            not isinstance(raw_expected_hdds, list)
            or len(raw_expected_hdds) > 64
            or any(
                isinstance(i, bool) or not isinstance(i, int) or not 1 <= i <= 256
                for i in raw_expected_hdds
            )
            or len(set(raw_expected_hdds)) != len(raw_expected_hdds)
        ):
            raise InventoryError(
                f"{label}: expected_sata_hdd_ids must be a unique list "
                "of positive SATA disk IDs (1-256)."
            )

        seen_ids.add(device_id)
        seen_ips.add(normalized_ip)

        result.append(
            NVRConfig(
                id=device_id,
                ip=normalized_ip,
                expected_channels=channel_count,
                http_port=http_port,
                server_port=server_port,
                recording_track_id=recording_track_id,
                recording_expectations=expectations,
                expected_sata_hdd_ids=tuple(raw_expected_hdds),
            )
        )

    return result
