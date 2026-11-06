"""Read-only probe: can this NVR do SMART, snapshots and real-time events?

  docker compose run --rm -T --entrypoint python nvr-report -m scripts.probe_nvr_features --nvr-id nvr-01

Only GET requests: device status, HDD list, SMART status per disk, one
picture from up to three online cameras, and the alert stream for
--seconds (default 30). Prints what worked and which .env line enables
each feature. Saves no pictures and prints no credentials or camera IPs.
"""
from __future__ import annotations

import argparse
import os
import time
from collections import Counter
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException

from app import events, smart, snapshots
from app.config import InventoryError, load_inventory
from app.isapi import get_channel_statuses, get_device_status
from app.storage import get_storage_check

ROOT = Path(__file__).resolve().parent.parent


def credentials(nvr) -> tuple[str, str]:
    suffix = nvr.id.upper().removeprefix("NVR-").replace("-", "_")
    return (os.environ.get(f"NVR_{suffix}_USERNAME", "").strip() or os.environ.get("NVR_USERNAME", "").strip(),
            os.environ.get(f"NVR_{suffix}_PASSWORD", "") or os.environ.get("NVR_PASSWORD", ""))


def probe_events(nvr, username: str, password: str, seconds: int) -> tuple[str, Counter]:
    url = f"http://{nvr.ip}:{nvr.http_port}/ISAPI/Event/notification/alertStream"
    counts: Counter = Counter()
    parser = events.StreamParser()
    deadline = time.monotonic() + seconds
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, auth=HTTPDigestAuth(username, password), stream=True,
                             timeout=(5, max(5, seconds)), allow_redirects=False) as response:
                if response.status_code != 200:
                    return f"HTTP_{response.status_code}", counts
                for chunk in response.iter_content(4096):
                    for event in parser.feed(chunk):
                        counts["heartbeat" if events.is_heartbeat(event) else event["type"] or "?"] += 1
                    if time.monotonic() >= deadline:
                        break
    except RequestException as exc:
        if counts:
            return "OK", counts
        return events.reason(exc), counts
    return "OK", counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nvr-id", required=True)
    parser.add_argument("--seconds", type=int, default=30, help="How long to listen to the event stream.")
    args = parser.parse_args()
    try:
        nvr = next((n for n in load_inventory(ROOT / "config" / "nvrs.local.json") if n.id == args.nvr_id), None)
    except InventoryError as exc:
        print(f"Cannot load inventory: {exc}")
        return 2
    if nvr is None:
        print("NVR ID is not in the inventory.")
        return 2
    username, password = credentials(nvr)
    if not username or not password:
        print("Credentials for this NVR are not configured in .env.")
        return 2
    enable = []
    print(f"Probing {nvr.id} (read-only)\n")

    status = get_device_status(nvr.ip, username, password, nvr.http_port)
    print(f"1. Device status: {status.status}"
          + (f", uptime {status.uptime_seconds // 86400} d {status.uptime_seconds % 86400 // 3600} h"
             if status.uptime_seconds is not None else ""))

    storage = get_storage_check(nvr.ip, username, password, nvr.http_port)
    disk_ids = [d.id.split(" (", 1)[0] for d in storage.disks
                if d.disk_type.strip().lower() == "sata" and d.status in {"ok", "normal", "idle"}
                and d.id.split(" (", 1)[0].isdecimal()]
    result = smart.check_smart(nvr.ip, nvr.http_port, username, password, disk_ids,
                               deadline=time.monotonic() + 120)
    print(f"2. Disk SMART: {result['status']} ({len(disk_ids)} installed SATA disks)")
    for disk in result["disks"]:
        print(f"   HDD {disk['id']}: {disk['status']} - {disk['message']}")
    if any(d["status"] in {"HEALTHY", "WARNING", "CRITICAL"} for d in result["disks"]):
        enable.append(f"NVR_SMART_NVR_IDS=...,{nvr.id}")

    channels = get_channel_statuses(nvr.ip, username, password, nvr.http_port)
    online = [c.channel_id for c in channels.channels if c.online][:3]
    print(f"3. Snapshots (up to 3 online cameras of {len(channels.channels)}):")
    worked = 0
    with requests.Session() as session:
        session.trust_env = False
        auth = HTTPDigestAuth(username, password)
        for cid in online:
            stream = snapshots.stream_id(cid, None)
            code, jpeg = snapshots.fetch(session, f"http://{nvr.ip}:{nvr.http_port}", auth, stream)
            if code == "OK" and jpeg:
                try:
                    metrics = snapshots.analyze(jpeg)
                    worked += 1
                    print(f"   ch{cid}: OK, {len(jpeg) // 1024} KB, {metrics['width']}x{metrics['height']}, "
                          f"brightness {metrics['brightness']}, contrast {metrics['contrast']}, {metrics['mode']}")
                except Exception as exc:
                    print(f"   ch{cid}: picture received but not decoded ({type(exc).__name__})")
            else:
                print(f"   ch{cid}: {code}")
    if worked:
        enable.append(f"NVR_SNAPSHOT_NVR_IDS=...,{nvr.id}")

    print(f"4. Real-time event stream, listening {args.seconds} s:")
    code, counts = probe_events(nvr, username, password, args.seconds)
    print(f"   {code}; received: " + (", ".join(f"{k} x{v}" for k, v in counts.most_common()) or "nothing"))
    known = sorted(k for k in counts if k in events.EVENT_TYPES)
    if known:
        print("   Events that would be reported: " + ", ".join(known))
    if code == "OK" and counts:
        enable.append(f"NVR_EVENT_NVR_IDS=...,{nvr.id}")

    print("\nTo enable what worked, add the NVR ID to these lines in .env:")
    for line in enable or ["(nothing worked; keep these features off for this NVR)"]:
        print("   " + line)
    return 0 if enable else 1


if __name__ == "__main__":
    raise SystemExit(main())
