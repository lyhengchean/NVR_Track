"""Scanner service: scheduled scans plus on-demand scans requested from the dashboard.

Runs inside the nvr-scanner container, which has the NVR credentials. The
dashboard never does: it only drops a small request file into the shared
scan-state folder, and this service validates it against the private
inventory before running the existing, read-only reporters.

Files in SCAN_STATE_DIR (default /app/state/scan):
  request.json   written by the dashboard, consumed here (one pending request)
  status.json    written here only; read by the dashboard
  schedule.json  last scheduled slot that ran, so restarts never repeat it

Nothing here contacts an NVR directly, and no device output, IP address or
credential is written to status.json.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import InventoryError, load_inventory
from scripts.run_all_nvr_reports import report_timeout_seconds

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("SCAN_STATE_DIR", str(ROOT / "state" / "scan")))
INVENTORY_PATH = ROOT / "config" / "nvrs.local.json"
TZ = ZoneInfo("Asia/Phnom_Penh")

POLL_SECONDS = 2
HEARTBEAT_SECONDS = 10
CATCH_UP_MINUTES = 30        # run a slot missed by a short restart, never older ones
REQUEST_MAX_AGE_MINUTES = 60  # a request left while the service was down is dropped
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
STARTING = re.compile(r"^Starting (\S+) at ", re.M)
TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ---------- settings ----------

def parse_times(text: str, default: str) -> list[tuple[int, int]]:
    """'06:00,08:00' -> sorted unique (hour, minute); invalid entries ignored."""
    times = set()
    for part in (text if text.strip() else default).split(","):
        match = TIME.match(part.strip())
        if match:
            times.add((int(match[1]), int(match[2])))
        elif part.strip():
            print(f"Ignoring invalid scan time {part.strip()!r}; use HH:MM.", flush=True)
    return sorted(times)


def settings() -> dict:
    def integer(name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(os.environ.get(name, default))
            return value if low <= value <= high else default
        except ValueError:
            return default
    return {
        "scan_times": parse_times(os.environ.get("NVR_SCAN_TIMES", ""), "08:00"),
        "email_times": parse_times(os.environ.get("NVR_EMAIL_TIMES", ""), "08:00"),
        "on_demand": os.environ.get("NVR_ON_DEMAND_SCANS", "true").strip().lower() == "true",
        "cooldown": integer("NVR_SCAN_COOLDOWN_SECONDS", 60, 0, 3600),
    }


# ---------- schedule ----------

def latest_slot(now: datetime, times: list[tuple[int, int]]) -> datetime | None:
    """Most recent scheduled time at or before now (today or yesterday)."""
    local = now.astimezone(TZ)
    candidates = [
        datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
        for day in (local.date(), local.date() - timedelta(days=1))
        for h, m in times
    ]
    past = [slot for slot in candidates if slot <= local]
    return max(past) if past else None


def next_slot(now: datetime, times: list[tuple[int, int]]) -> datetime | None:
    local = now.astimezone(TZ)
    candidates = [
        datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
        for day in (local.date(), local.date() + timedelta(days=1))
        for h, m in times
    ]
    future = [slot for slot in candidates if slot > local]
    return min(future) if future else None


def due_slot(now: datetime, times: list[tuple[int, int]], last_run: datetime | None) -> datetime | None:
    """The slot to run now, if any. Slots older than CATCH_UP_MINUTES are skipped."""
    slot = latest_slot(now, times)
    if slot is None or (last_run is not None and slot <= last_run):
        return None
    if now.astimezone(TZ) - slot > timedelta(minutes=CATCH_UP_MINUTES):
        return None
    return slot


# ---------- files ----------

def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix="." + path.name, suffix=".tmp", delete=False) as handle:
        json.dump(data, handle, ensure_ascii=True, separators=(",", ":"))
        temporary = Path(handle.name)
    os.replace(temporary, path)


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def take_request(inventory_ids: set[str], now: datetime) -> tuple[dict | None, str | None]:
    """Consume request.json. Returns (job, rejection reason)."""
    path = STATE_DIR / "request.json"
    if not path.exists():
        return None, None
    request = read_json(path)
    path.unlink(missing_ok=True)
    if request is None or request.get("schema_version") != 1:
        return None, "Unreadable scan request."
    target = request.get("target")
    try:
        requested = datetime.fromisoformat(request.get("requested_at"))
        if requested.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        return None, "Scan request has no valid time."
    if now - requested > timedelta(minutes=REQUEST_MAX_AGE_MINUTES):
        return None, "Scan request expired before the scanner saw it."
    if target != "all" and (not isinstance(target, str) or not DEVICE_ID.fullmatch(target)
                            or target not in inventory_ids):
        return None, "Requested NVR is not in the inventory."
    request_id = request.get("id") if isinstance(request.get("id"), str) else ""
    return {"id": request_id[:40] or uuid.uuid4().hex, "target": target, "trigger": "manual",
            "email": False, "requested_at": iso(requested)}, None


# ---------- running ----------

def command_for(job: dict, nvrs: list) -> list[str]:
    """Fixed argument lists only; nothing from the request reaches a shell."""
    if job["target"] == "all":
        return [sys.executable, "-m", "scripts.run_all_nvr_reports"] + ([] if job["email"] else ["--no-email"])
    nvr = next(n for n in nvrs if n.id == job["target"])
    return [sys.executable, "-m", "scripts.run_nvr_report", "--ip", nvr.ip, "--http-port", str(nvr.http_port)]


def summarize_output(job: dict, output: str, returncode: int | None) -> dict:
    """Fixed codes only, parsed from the reporters' own summary lines."""
    if returncode is None:
        return {"result": "TIMEOUT"}
    if job["target"] == "all":
        errors = re.search(r"Execution errors: (\d+)", output)
        email = re.search(r"^Email report: (SENT|SKIPPED|FAILED|NOT SENT)", output, re.M)
        return {"result": "OK" if errors and errors[1] == "0" else "ERRORS" if errors else "FAILED",
                "execution_errors": int(errors[1]) if errors else None,
                "email_result": email[1] if email else None}
    if "Another report is already running" in output:
        return {"result": "BUSY"}
    overall = re.search(r"^Overall status: (HEALTHY|WARNING|CRITICAL|UNKNOWN)$", output, re.M)
    saved = "JSON report saved:" in output
    return {"result": "OK" if saved else "FAILED", "overall": overall[1] if overall else None}


class Service:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status: dict = {"schema_version": 1, "current": None, "last": None,
                             "last_email": None, "rejected": None, "cooldown_until": None}

    def publish(self, **changes) -> None:
        with self.lock:
            self.status.update(changes)
            config = settings()
            self.status.update({
                "heartbeat_at": iso(datetime.now(timezone.utc)),
                "on_demand_enabled": config["on_demand"],
                "scan_times": [f"{h:02d}:{m:02d}" for h, m in config["scan_times"]],
                "email_times": [f"{h:02d}:{m:02d}" for h, m in config["email_times"]],
                "next_scheduled_at": iso(next_slot(datetime.now(timezone.utc), config["scan_times"])),
            })
            write_json(STATE_DIR / "status.json", self.status)

    def heartbeat(self) -> None:
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            try:
                self.publish()
            except OSError:
                pass

    def run(self, job: dict, nvrs: list) -> None:
        started = datetime.now(timezone.utc)
        total = len(nvrs) if job["target"] == "all" else 1
        current = {**job, "state": "running", "started_at": iso(started),
                   "progress": {"done": 0, "total": total, "nvr": None if job["target"] == "all" else job["target"]}}
        self.publish(current=current)
        label = "all NVRs" if job["target"] == "all" else job["target"]
        print(f"{started.astimezone(TZ):%Y-%m-%d %H:%M} {job['trigger']} scan of {label}"
              f"{' with email' if job['email'] else ''} started", flush=True)

        timeout = report_timeout_seconds() * total + 300
        process = subprocess.Popen(command_for(job, nvrs), cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        killer = threading.Timer(timeout, process.kill)
        killer.start()
        lines = []
        try:
            for line in process.stdout:
                lines.append(line)
                match = STARTING.match(line)
                if match and DEVICE_ID.fullmatch(match[1]):
                    current["progress"] = {"done": current["progress"]["done"] + 1, "total": total, "nvr": match[1]}
                    self.publish(current=current)
            returncode = process.wait()
        finally:
            timed_out = not killer.is_alive()
            killer.cancel()
        outcome = summarize_output(job, "".join(lines), None if timed_out else returncode)
        finished = datetime.now(timezone.utc)
        last = {key: job[key] for key in ("id", "target", "trigger", "email", "requested_at")}
        last.update(outcome, started_at=iso(started), finished_at=iso(finished))
        changes = {"current": None, "last": last}
        if job["email"]:
            changes["last_email"] = {"result": outcome.get("email_result"), "finished_at": iso(finished)}
        if job["trigger"] == "manual":
            changes["cooldown_until"] = iso(finished + timedelta(seconds=settings()["cooldown"]))
        self.publish(**changes)
        print(f"{finished.astimezone(TZ):%Y-%m-%d %H:%M} {job['trigger']} scan of {label} finished: "
              + ", ".join(f"{k}={v}" for k, v in outcome.items()), flush=True)

    def loop(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        previous = read_json(STATE_DIR / "status.json") or {}
        for key in ("last", "last_email"):
            if isinstance(previous.get(key), dict):
                self.status[key] = previous[key]
        if isinstance(previous.get("current"), dict):
            interrupted = dict(previous["current"], state="interrupted", result="INTERRUPTED",
                               finished_at=iso(datetime.now(timezone.utc)))
            self.status["last"] = interrupted
        self.publish(current=None)
        threading.Thread(target=self.heartbeat, daemon=True).start()

        schedule_state = read_json(STATE_DIR / "schedule.json") or {}
        try:
            last_slot = datetime.fromisoformat(schedule_state["last_slot"])
        except (KeyError, TypeError, ValueError):
            # First start: do not run a slot that cron (or an older version) already ran.
            last_slot = latest_slot(datetime.now(timezone.utc), settings()["scan_times"])
            write_json(STATE_DIR / "schedule.json", {"last_slot": iso(last_slot)})
        print("Scanner ready. Scan times: " + ", ".join(self.status["scan_times"])
              + "; email at: " + ", ".join(self.status["email_times"]), flush=True)

        while True:
            now = datetime.now(timezone.utc)
            config = settings()
            try:
                nvrs = load_inventory(INVENTORY_PATH)
            except InventoryError as exc:
                print(f"Cannot load inventory: {exc}", flush=True)
                time.sleep(30)
                continue
            slot = due_slot(now, config["scan_times"], last_slot)
            if slot is not None:
                last_slot = slot
                write_json(STATE_DIR / "schedule.json", {"last_slot": iso(slot)})
                local = slot.astimezone(TZ)
                job = {"id": uuid.uuid4().hex, "target": "all", "trigger": "schedule",
                       "email": (local.hour, local.minute) in config["email_times"],
                       "requested_at": iso(slot)}
                self.run(job, nvrs)
                continue
            if config["on_demand"]:
                job, rejected = take_request({n.id for n in nvrs}, now)
                if rejected:
                    self.publish(rejected={"reason": rejected, "at": iso(now)})
                    print(f"Scan request rejected: {rejected}", flush=True)
                if job:
                    self.run(job, nvrs)
                    continue
            else:
                (STATE_DIR / "request.json").unlink(missing_ok=True)
            time.sleep(POLL_SECONDS)


def main() -> int:
    Service().loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
