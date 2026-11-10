"""nvr-live service: camera online/offline, disk states and NVR reachability
every NVR_LIVE_SECONDS (default 60) for the NVRs in NVR_LIVE_NVR_IDS.

Per check and NVR: GET /ISAPI/ContentMgmt/InputProxy/channels/status and
GET /ISAPI/ContentMgmt/Storage/hdd, the same read-only requests every scan
makes. Never sends POST/PUT/DELETE, never changes NVR settings, never prints
credentials. Writes state/live/<nvr>.json and state/live/service.json; the
dashboard and the Telegram bot read them (nvr_track.monitoring.live).
"""
from __future__ import annotations

import os
import sys
import threading
import time

from nvr_track.monitoring import flapping, live
from nvr_track.core.config import InventoryError, load_inventory
from nvr_track.nvr.isapi import get_channel_statuses
from nvr_track.nvr.storage import classify_storage, get_storage_check
from nvr_track.services.event_listener import credentials
from nvr_track.core.paths import PROJECT_ROOT as ROOT

INVENTORY_PATH = ROOT / "config" / "nvrs.local.json"


def window_minutes() -> int:
    """The scans' recording window (a full disk counts as overwritten if they saw footage)."""
    try:
        value = int(os.environ.get("NVR_RECORDING_WINDOW_MINUTES", "60"))
    except ValueError:
        return 60
    return value if 1 <= value <= 1440 else 60


def check(nvr, previous: dict | None, interval: int, flap_hours: int, minutes: int) -> dict:
    """One live check of one NVR (two GETs); returns the new entry."""
    username, password = credentials(nvr)
    channels = get_channel_statuses(nvr.ip, username, password, nvr.http_port)
    storage = get_storage_check(nvr.ip, username, password, nvr.http_port,
                                expected_sata_hdd_ids=nvr.expected_sata_hdd_ids,
                                empty_sata_hdd_ids=nvr.empty_sata_hdd_ids)
    writing = None
    if storage.disks:
        status, message = classify_storage(storage.disks, expected_sata_hdd_ids=nvr.expected_sata_hdd_ids,
                                           empty_sata_hdd_ids=nvr.empty_sata_hdd_ids,
                                           recent_write_minutes=minutes)
        writing = storage.__class__(status, storage.http_status, storage.disks, message)
    return live.next_entry(previous, nvr.id, interval, channels, storage, writing, flap_hours=flap_hours)


def describe(entry: dict, previous: dict | None) -> str | None:
    """One log line when something changed (no IPs, no credentials)."""
    previous = previous or {}
    nvr = entry["nvr"]
    if entry["reachable"] is False and entry["failures"] == live.DOWN_AFTER_FAILURES:
        return f"live: {nvr} not reachable ({entry['error']})"
    if entry["reachable"] is True and previous.get("reachable") is False:
        return f"live: {nvr} reachable again"
    if entry["reachable"] is None and previous.get("error") != entry["error"]:
        return f"live: {nvr} answered but could not be read ({entry['error']})"
    if entry["channels_ok"] and previous.get("channels"):
        went = [c for c, s in entry["channels"].items() if s == "OFFLINE" and previous["channels"].get(c) == "ONLINE"]
        back = [c for c, s in entry["channels"].items() if s == "ONLINE" and previous["channels"].get(c) == "OFFLINE"]
        if went or back:
            return (f"live: {nvr}" + (f" offline ch{', ch'.join(sorted(went, key=_order))}" if went else "")
                    + (f" back online ch{', ch'.join(sorted(back, key=_order))}" if back else ""))
    return None


def _order(value: str) -> int:
    return int(value) if value.isdigit() else 10**6


class Poller:
    def __init__(self, nvr, interval: int, flap_hours: int, minutes: int):
        self.nvr, self.interval, self.flap_hours, self.minutes = nvr, interval, flap_hours, minutes

    def run(self) -> None:
        username, password = credentials(self.nvr)
        if not username or not password:
            print(f"live: {self.nvr.id} has no credentials in .env; skipped", flush=True)
            return
        previous = live.read_entry(self.nvr.id)
        while True:
            started = time.monotonic()
            try:
                entry = check(self.nvr, previous, self.interval, self.flap_hours, self.minutes)
                live.write_entry(entry)
                line = describe(entry, previous)
                if line:
                    print(line, flush=True)
                previous = entry
            except OSError as exc:
                print(f"live: {self.nvr.id} cannot save ({type(exc).__name__})", flush=True)
            except Exception as exc:  # Keep the thread alive; never print request details.
                print(f"live: {self.nvr.id} check failed ({type(exc).__name__})", flush=True)
            time.sleep(max(5.0, self.interval - (time.monotonic() - started)))


def remove_unlisted(keep: set[str]) -> None:
    """Drop entries of NVRs no longer in NVR_LIVE_NVR_IDS (state/live only)."""
    try:
        paths = list(live.live_dir().glob("*.json"))
    except OSError:
        return
    for path in paths:
        if path.name != "service.json" and path.stem not in keep and not path.is_symlink():
            try:
                path.unlink()
            except OSError:
                pass


def main() -> int:
    enabled = live.selected_ids()
    interval = live.interval_seconds()
    try:
        nvrs = [n for n in load_inventory(INVENTORY_PATH) if n.id.lower() in enabled]
    except InventoryError as exc:
        print(f"live: cannot load inventory: {exc}", flush=True)
        return 2
    remove_unlisted({n.id for n in nvrs})
    missing = sorted(enabled - {n.id.lower() for n in nvrs})
    if missing:
        print(f"live: not in the inventory, skipped: {', '.join(missing)}", flush=True)
    if not nvrs:
        print("live: no NVR in NVR_LIVE_NVR_IDS; idle. Set it in .env (e.g. NVR_LIVE_NVR_IDS=nvr-04) "
              "and recreate nvr-live.", flush=True)
    flap_hours = flapping.settings()["hours"]
    for nvr in nvrs:
        poller = Poller(nvr, interval, flap_hours, window_minutes())
        threading.Thread(target=poller.run, daemon=True, name=nvr.id).start()
    if nvrs:
        print(f"live: checking {', '.join(n.id for n in nvrs)} every {interval} s "
              "(channel status and disks, read-only)", flush=True)
    while True:
        try:
            live.write_service([n.id for n in nvrs], interval)  # Heartbeat for the Docker healthcheck.
        except OSError as exc:
            print(f"live: cannot write status ({type(exc).__name__})", file=sys.stderr, flush=True)
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
