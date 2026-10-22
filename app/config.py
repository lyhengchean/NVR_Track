"""Load and validate the NVR inventory without contacting devices."""

import json
import re
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path


class InventoryError(ValueError):
    """Raised when the NVR inventory is invalid."""


@dataclass(frozen=True)
class NVRConfig:
    id: str
    ip: str
    expected_channels: int


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

        seen_ids.add(device_id)
        seen_ips.add(normalized_ip)

        result.append(
            NVRConfig(
                id=device_id,
                ip=normalized_ip,
                expected_channels=channel_count,
            )
        )

    return result
