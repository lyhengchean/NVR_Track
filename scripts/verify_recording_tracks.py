"""Read-only recording-track inventory audit. No recording searches or writes."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import InventoryError, load_inventory
from app.isapi import get_channel_inventory
from app.camera_recording import get_tracks, resolve_track

ROOT = Path(__file__).resolve().parent.parent


def verify_one(nvr: object, username: str, password: str) -> dict:
    """Audit track mapping against the device's real inventory and track list."""
    inventory = get_channel_inventory(nvr.ip, username, password, nvr.http_port)
    response = get_tracks(nvr.ip, nvr.http_port, username, password)
    result = {
        "nvr_id": nvr.id,
        "checked_at": datetime.now(ZoneInfo("Asia/Phnom_Penh")).isoformat(),
        "inventory_status": inventory.status,
        "track_list_status": response["status"],
        "track_count": len(response["tracks"]) if response["status"] == "VERIFIED" else None,
        "channels": [],
    }
    if inventory.status != "CHANNELS_RETRIEVED":
        return result
    for ch in inventory.channels:
        if response["status"] == "VERIFIED":
            status, track_id, explanation = resolve_track(ch.channel_id, response["tracks"])
        else:
            status, track_id, explanation = (
                "TRACK_MAPPING_UNVERIFIED", None, "Track inventory unavailable."
            )
        result["channels"].append({
            "channel_id": ch.channel_id,
            "track_id": track_id,
            "status": status,
            "message": explanation,
        })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Hikvision track mappings (GET-only).")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--nvr-id", help="NVR ID, e.g. nvr-01")
    selection.add_argument("--all", action="store_true", help="Check all inventoried NVRs, sequentially")
    args = parser.parse_args()
    try:
        nvrs = load_inventory(ROOT / "config" / "nvrs.local.json")
    except InventoryError as exc:
        print(f"Cannot load local inventory: {exc}")
        return 2
    selected = nvrs if args.all else [nvr for nvr in nvrs if nvr.id == args.nvr_id]
    if not selected:
        print("NVR ID is not in the private inventory.")
        return 2
    report = []
    failure = False
    for nvr in selected:
        suffix = nvr.id.upper().removeprefix("NVR-").replace("-", "_")
        username = os.environ.get(f"NVR_{suffix}_USERNAME", "").strip() or os.environ.get("NVR_USERNAME", "").strip()
        password = os.environ.get(f"NVR_{suffix}_PASSWORD", "") or os.environ.get("NVR_PASSWORD", "")
        if not username or not password:
            print(f"{nvr.id}: SKIPPED, credentials not configured.")
            report.append({"nvr_id": nvr.id, "track_list_status": "NOT_CONFIGURED", "channels": []})
            failure = True
            continue
        checked = verify_one(nvr, username, password)
        total = len(checked["channels"])
        verified = sum(ch["status"] == "VERIFIED" for ch in checked["channels"])
        print(f"{nvr.id}: tracks={checked['track_list_status']}, "
              f"camera mappings verified={verified}/{total}, "
              f"inventory={checked['inventory_status']}")
        if checked["track_list_status"] != "VERIFIED" or verified != total:
            failure = True
        report.append(checked)
    output_dir = ROOT / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = "track-verification-" + datetime.now(ZoneInfo("Asia/Phnom_Penh")).strftime("%Y%m%dT%H%M%S%f%z") + ".json"
    output = output_dir / filename
    output.write_text(json.dumps({"nvrs": report}, indent=2), encoding="utf-8")
    print(f"Mapping audit saved: {output}")
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
