"""Read-only probe: can this NVR do SMART, snapshots and real-time events?

  docker compose run --rm -T --entrypoint python nvr-report -m nvr_track.cli.probe_nvr_features --nvr-id nvr-01

Only GET requests: device status, HDD list, SMART status per disk, one
picture from up to three online cameras, and the alert stream for
--seconds (default 30). Prints what worked and which .env line enables
each feature. Saves no pictures and prints no credentials or camera IPs.

Trying a new NVR login or HTTPS on one NVR first (nothing is saved, .env is
not changed; run WITHOUT -T so the password can be typed hidden):

  docker compose run --rm --entrypoint python nvr-report -m nvr_track.cli.probe_nvr_features \
      --nvr-id nvr-02 --ask-login --https

--ask-login  type a login instead of the one in .env, and run one full
             read-only scan with it into a temporary folder (--scan); lists
             every NVR answer 401/403 by check, and checks left unverified.
             Stops after step 1 if the NVR refuses the login.
--scan       the same full scan with the .env login (the scanner's usual
             GETs and recording searches; nothing reaches reports/ or state/).
--https      read the NVR's HTTPS certificate (SHA-256 fingerprint, expiry)
             and repeat two GETs over HTTPS pinned to that certificate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections import Counter
from getpass import getpass
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException

from nvr_track.nvr import events, smart, snapshots
from nvr_track.core.config import InventoryError, load_inventory
from nvr_track.nvr.isapi import get_channel_statuses, get_device_status
from nvr_track.nvr.storage import get_storage_check
from nvr_track.core.paths import PROJECT_ROOT as ROOT, child_env

# What each NVR request is for, to name refusals in plain words.
CHECK_NAMES = [
    ("GET", r"/ISAPI/System/deviceInfo", "device information"),
    ("GET", r"/ISAPI/System/status", "uptime and restarts"),
    ("GET", r"/ISAPI/System/time", "NVR clock"),
    ("GET", r"/ISAPI/ContentMgmt/InputProxy/channels", "camera list"),
    ("GET", r"/ISAPI/ContentMgmt/InputProxy/channels/status", "camera online status"),
    ("GET", r"/ISAPI/ContentMgmt/Storage(/hdd)?", "disks and free space"),
    ("GET", r"/ISAPI/ContentMgmt/Storage/hdd/\d+/SMARTTest/status", "disk SMART"),
    ("GET", r"/ISAPI/ContentMgmt/record/tracks", "recording tracks"),
    ("GET", r"/ISAPI/ContentMgmt/search/profile", "recording search support"),
    ("POST", r"/ISAPI/ContentMgmt/search", "recording search (playback right)"),
    ("POST", r"/ISAPI/ContentMgmt/record/tracks/\d+/dailyDistribution", "30-day recording calendar (playback right)"),
    ("GET", r"/ISAPI/Streaming/channels/\d+/picture", "camera pictures (live view right)"),
]
# NVR_INVENTORY_PATH: another inventory file (tests); default config/nvrs.local.json.
INVENTORY_PATH = Path(os.environ.get("NVR_INVENTORY_PATH") or ROOT / "config" / "nvrs.local.json")


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


def env_suffix(nvr) -> str:
    return nvr.id.upper().removeprefix("NVR-").replace("-", "_")


def ask_login() -> tuple[str, str] | None:
    if not sys.stdin.isatty():
        print("--ask-login needs a terminal: run docker compose run WITHOUT -T.")
        return None
    username = input("NVR username to try: ").strip()
    password = getpass("Password (input hidden): ")
    return (username, password) if username and password else None


def check_name(method: str, path: str) -> str:
    for known_method, pattern, name in CHECK_NAMES:
        if method == known_method and re.fullmatch(pattern, path):
            return name
    return f"{method} {re.sub(r'/[0-9]+(?=/|$)', '/<n>', path)}"


def saved_unknown_checks(nvr) -> list[str] | None:
    """Checks left unverified in this NVR's newest saved report (the current login), if any."""
    for path in sorted((ROOT / "reports").glob(f"{nvr.id}_*.json"), reverse=True)[:1]:
        try:
            return list(json.loads(path.read_text(encoding="utf-8"))["overall"].get("unknown_checks") or [])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
    return None


def scan_with_login(nvr, username: str, password: str) -> int:
    """One full read-only scan into a temporary folder; nothing reaches reports/ or state/."""
    print("5. Full read-only scan with this login (not saved; can take a few minutes):")
    with tempfile.TemporaryDirectory(prefix="nvr-probe-") as temp:
        refusals_log = Path(temp) / "refusals.jsonl"
        env = child_env()
        env.update({f"NVR_{env_suffix(nvr)}_USERNAME": username, f"NVR_{env_suffix(nvr)}_PASSWORD": password,
                    "SNAPSHOT_DIR": str(Path(temp) / "snapshots"), "NVR_PROBE_REFUSALS": str(refusals_log)})
        try:
            result = subprocess.run(
                [sys.executable, "-m", "nvr_track.cli.probe_login_scan", "--ip", nvr.ip,
                 "--http-port", str(nvr.http_port), "--report-dir", str(Path(temp) / "reports")],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            print("   Not verified: the scan did not finish in 15 minutes.")
            return 1
        for line in result.stdout.splitlines():
            if line.startswith(("Overall status:", "Device information:", "Channel inventory:", "Storage:",
                                "Per-camera recording:", "Recording calendar:")):
                print("   " + line)
        reports = sorted((Path(temp) / "reports").glob(f"{nvr.id}_*.json"))
        if not reports:
            print(f"   Not verified: no report written (exit code {result.returncode}); last line: "
                  + (result.stdout.strip().splitlines() or ["-"])[-1][:160])
            return 1
        try:
            report = json.loads(reports[-1].read_text(encoding="utf-8"))
            unknown = list(report["overall"].get("unknown_checks") or [])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            print("   Not verified: the report could not be read.")
            return 1
        refused: Counter = Counter()
        if refusals_log.exists():
            for line in refusals_log.read_text(encoding="utf-8").splitlines():
                method, path, status = json.loads(line)
                refused[(check_name(method, path), status)] += 1
    if refused:
        print("   Refused by the NVR for this login:")
        for (name, status), count in sorted(refused.items()):
            print(f"   - {name}: HTTP {status} x{count}")
        print("   Give this NVR user the missing right, or keep the current login for this NVR.")
        return 1
    saved = saved_unknown_checks(nvr)
    new = [item for item in unknown if saved is None or item not in saved]
    if new:
        print("   Nothing refused, but these checks did not complete in this scan:")
        for item in new:
            print("   - " + item)
        print("   Compare with this NVR's last scan on the dashboard; switch only when this scan matches it.")
        return 1
    if unknown:
        print("   Nothing refused. Unverified here as in the last saved scan (not caused by this login): "
              + "; ".join(unknown))
    else:
        print("   Nothing refused and nothing left unverified.")
    print("   This login can do every check the scanner runs on this NVR (steps 2-4 show SMART, pictures, events).")
    return 0


class PinnedAdapter(HTTPAdapter):
    """HTTPS that trusts exactly one certificate (by SHA-256), as NVRs use self-signed ones."""

    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        kwargs["assert_fingerprint"] = self.fingerprint
        super().init_poolmanager(*args, **kwargs)


def certificate_text(der: bytes) -> str:
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as handle:
            handle.write(ssl.DER_cert_to_PEM_cert(der))
        info = ssl._ssl._test_decode_cert(handle.name)      # Reads a certificate file; no network.
        subject = dict(item[0] for item in info.get("subject", ()))
        return f"subject CN={subject.get('commonName', '?')}, valid until {info.get('notAfter', '?')}"
    except Exception:
        return "certificate details not readable"
    finally:
        try:
            os.unlink(handle.name)
        except (OSError, UnboundLocalError):
            pass


def https_check(nvr, port: int, username: str, password: str) -> int:
    print(f"6. HTTPS on port {port}:")
    reader = ssl.create_default_context()
    reader.check_hostname = False
    reader.verify_mode = ssl.CERT_NONE          # Only to read the certificate; the GETs below are pinned.
    try:
        with socket.create_connection((nvr.ip, port), timeout=5) as raw, \
                reader.wrap_socket(raw, server_hostname=nvr.ip) as tls:
            der = tls.getpeercert(binary_form=True)
            version, cipher = tls.version(), (tls.cipher() or ("?",))[0]
    except (OSError, ssl.SSLError) as exc:
        print(f"   not usable: {type(exc).__name__} ({getattr(exc, 'reason', None) or exc.__class__.__doc__ or ''})"[:200])
        return 1
    fingerprint = hashlib.sha256(der).hexdigest()
    print(f"   {version}, {cipher}")
    print(f"   certificate SHA-256 {':'.join(fingerprint[i:i + 2] for i in range(0, 64, 2)).upper()}")
    print(f"   {certificate_text(der)}")
    worked = 0
    with requests.Session() as session:
        session.trust_env = False
        session.verify = False                  # Replaced by the pin: only this certificate is accepted.
        session.mount("https://", PinnedAdapter(fingerprint))
        auth = HTTPDigestAuth(username, password)
        for path in ("/ISAPI/System/deviceInfo", "/ISAPI/ContentMgmt/InputProxy/channels/status"):
            try:
                response = session.get(f"https://{nvr.ip}:{port}{path}", auth=auth, timeout=(5, 20),
                                       allow_redirects=False)
                print(f"   GET {path}: HTTP {response.status_code}")
                worked += response.status_code == 200
            except RequestException as exc:
                print(f"   GET {path}: {type(exc).__name__}")
    if worked == 2:
        print("   HTTPS works with this certificate. Keep the fingerprint above for the next stage.")
        return 0
    print("   HTTPS is not usable for the checks yet.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--nvr-id", required=True)
    parser.add_argument("--seconds", type=int, default=30, help="How long to listen to the event stream.")
    parser.add_argument("--ask-login", action="store_true", help="Type a login to try instead of the .env one (implies --scan).")
    parser.add_argument("--scan", action="store_true", help="Also run one full read-only scan into a temporary folder.")
    parser.add_argument("--https", action="store_true", help="Check the NVR's HTTPS port and certificate.")
    parser.add_argument("--https-port", type=int, default=443)
    args = parser.parse_args()
    try:
        nvr = next((n for n in load_inventory(INVENTORY_PATH) if n.id == args.nvr_id), None)
    except InventoryError as exc:
        print(f"Cannot load inventory: {exc}")
        return 2
    if nvr is None:
        print("NVR ID is not in the inventory.")
        return 2
    if args.ask_login:
        login = ask_login()
        if login is None:
            print("No login typed.")
            return 2
        username, password = login
        print("Using the typed login for this probe only; .env is not changed.")
    else:
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
    if status.status == "AUTHENTICATION_FAILED":
        print("   Login refused (HTTP 401): wrong username or password, or this user may not log in remotely.\n"
              "   Stopped here so repeated failed logins cannot lock this server out of the NVR.")
        return 1

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

    failed = 0
    if args.ask_login or args.scan:
        print()
        failed += scan_with_login(nvr, username, password)
    if args.https:
        print()
        failed += https_check(nvr, args.https_port, username, password)

    print("\nTo enable what worked, add the NVR ID to these lines in .env:")
    for line in enable or ["(nothing worked; keep these features off for this NVR)"]:
        print("   " + line)
    return 0 if enable and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
