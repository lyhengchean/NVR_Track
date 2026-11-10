"""Command-line entry point for validating the NVR inventory."""

import os
import sys
from pathlib import Path

from nvr_track.core.config import InventoryError, load_inventory
from nvr_track.core.paths import PROJECT_ROOT


def main() -> int:
    project_root = PROJECT_ROOT

    configured_path = os.environ.get("NVR_CONFIG_PATH")
    if configured_path:
        inventory_path = Path(configured_path)
        if not inventory_path.is_absolute():
            inventory_path = project_root / inventory_path
    else:
        inventory_path = project_root / "config" / "nvrs.local.json"

    try:
        nvrs = load_inventory(inventory_path)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    total_channels = sum(nvr.expected_channels for nvr in nvrs)

    print("Hikvision CCTV Monitor - Inventory Validation")
    print(f"NVR count: {len(nvrs)}")
    print(f"Expected channel total: {total_channels}")

    for nvr in nvrs:
        print(f"- {nvr.id}: expected {nvr.expected_channels} channels")

    print("Inventory validation: PASS")
    print("No NVR network requests were made.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
