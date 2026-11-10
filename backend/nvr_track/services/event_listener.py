"""nvr-events service: real-time Hikvision events, read-only.

One GET /ISAPI/Event/notification/alertStream connection per NVR listed in
NVR_EVENT_NVR_IDS (each in its own thread). Wanted events are appended to
state/events/events.jsonl; connection state goes to state/events/status.json.
Never sends POST/PUT/DELETE, never changes NVR settings, never prints
credentials. The Telegram bot and dashboard read the files.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timezone

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException
from urllib3.exceptions import HTTPError as Urllib3Error

from nvr_track.nvr import events
from nvr_track.core.config import InventoryError, load_inventory
from nvr_track.core.paths import PROJECT_ROOT as ROOT

INVENTORY_PATH = ROOT / "config" / "nvrs.local.json"
READ_TIMEOUT = 90          # Hikvision sends a heartbeat about every 10 s.
BACKOFF = (10, 30, 60, 120, 300)
CHUNK = 4096


def stream_chunks(response, size: int = CHUNK):
    """Yield stream data as soon as it arrives.

    response.iter_content(4096) waits until 4096 bytes are buffered; on an
    alert stream that carries a small heartbeat every ~10 s that held an
    event back for a minute or more. read1() returns whatever has arrived.
    Raw reads raise urllib3 errors (not requests ones); the caller catches them.
    """
    read1 = getattr(response.raw, "read1", None)
    if read1 is None:  # urllib3 1.x
        yield from response.iter_content(size)
        return
    while True:
        data = read1(size, decode_content=True)
        if not data:
            return
        yield data


def credentials(nvr) -> tuple[str, str]:
    suffix = nvr.id.upper().removeprefix("NVR-").replace("-", "_")
    username = os.environ.get(f"NVR_{suffix}_USERNAME", "").strip() or os.environ.get("NVR_USERNAME", "").strip()
    password = os.environ.get(f"NVR_{suffix}_PASSWORD", "") or os.environ.get("NVR_PASSWORD", "")
    return username, password


class Listener:
    def __init__(self, nvr, types: dict, dedupe: events.Deduplicator, lock: threading.Lock):
        self.nvr = nvr
        self.types = types
        self.dedupe = dedupe
        self.lock = lock
        self.state = {"connected": False, "since": None, "last_seen": None, "last_error": None,
                      "events": 0, "ignored_types": {}, "down_since": now()}

    def run(self) -> None:
        username, password = credentials(self.nvr)
        if not username or not password:
            self.state["last_error"] = "NO_CREDENTIALS"
            return
        url = f"http://{self.nvr.ip}:{self.nvr.http_port}/ISAPI/Event/notification/alertStream"
        failures = 0
        while True:
            parser = events.StreamParser()
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    with session.get(url, auth=HTTPDigestAuth(username, password), stream=True,
                                     timeout=(5, READ_TIMEOUT), allow_redirects=False,
                                     headers={"Accept": "*/*"}) as response:
                        if response.status_code != 200:
                            raise ConnectionError(f"HTTP_{response.status_code}")
                        self.mark(connected=True, since=now(), last_error=None, down_since=None)
                        failures = 0
                        opened = time.monotonic()
                        for chunk in stream_chunks(response):
                            self.state["last_seen"] = now()
                            for event in parser.feed(chunk):
                                self.handle(event, replay=time.monotonic() - opened < events.REPLAY_SECONDS)
                raise ConnectionError("STREAM_CLOSED")
            except (RequestException, Urllib3Error, OSError) as exc:
                code = events.reason(exc)
                if self.state["connected"]:
                    print(f"events: {self.nvr.id} disconnected ({code})", flush=True)
                elif self.state["last_error"] != code:
                    print(f"events: {self.nvr.id} cannot connect ({code}); retrying", flush=True)
                self.mark(connected=False, since=now(), last_error=code[:40],
                          down_since=self.state.get("down_since") or now())  # Start of this outage.
            wait = BACKOFF[min(failures, len(BACKOFF) - 1)]
            failures += 1
            time.sleep(wait)

    def mark(self, **changes) -> None:
        with self.lock:
            self.state.update(changes)

    def handle(self, event: dict, replay: bool = False) -> None:
        if events.is_heartbeat(event):
            return
        if event["type"] not in self.types:
            ignored = self.state["ignored_types"]
            if len(ignored) < 30 or event["type"] in ignored:
                ignored[event["type"]] = ignored.get(event["type"], 0) + 1
            return
        # NVR-level types are kept without a channel (as recorded), so a
        # restart recognises them in events.jsonl.
        key_event = {**event, "channel": event["channel"] if self.types[event["type"]][2] else ""}
        if not self.dedupe.accept(self.nvr.id, key_event, replay=replay):
            if replay:
                self.state["replayed"] = self.state.get("replayed", 0) + 1
            return
        with self.lock:
            try:
                line = events.record(self.nvr.id, event, self.types)
            except OSError as exc:
                print(f"events: cannot save event ({type(exc).__name__})", flush=True)
                return
            self.state["events"] += 1
        print(f"events: {self.nvr.id} {line['label']}" + (f" ch{line['channel']}" if line["channel"] else ""),
              flush=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    enabled = {i.strip().lower() for i in os.environ.get("NVR_EVENT_NVR_IDS", "").split(",") if i.strip()}
    try:
        nvrs = [n for n in load_inventory(INVENTORY_PATH) if n.id.lower() in enabled]
    except InventoryError as exc:
        print(f"events: cannot load inventory: {exc}", flush=True)
        return 2
    if not nvrs:
        print("events: no NVR in NVR_EVENT_NVR_IDS; idle. Run nvr_track.cli.probe_nvr_features first, "
              "then set it in .env and recreate nvr-events.", flush=True)
        while True:
            try:
                events.write_status({})  # Heartbeat for the Docker healthcheck.
            except OSError:
                pass
            time.sleep(60)
    try:
        repeat = int(os.environ.get("NVR_EVENT_REPEAT_MINUTES", "30"))
        repeat = repeat if 1 <= repeat <= 1440 else 30
    except ValueError:
        repeat = 30
    lock = threading.Lock()
    dedupe = events.Deduplicator(repeat, seen=events.recent_events(hours=events.REPLAY_HOURS, limit=5000))
    types = events.wanted_types()
    listeners = [Listener(nvr, types, dedupe, lock) for nvr in nvrs]
    for listener in listeners:
        threading.Thread(target=listener.run, daemon=True, name=listener.nvr.id).start()
    print(f"events: listening to {', '.join(n.id for n in nvrs)}; types: {', '.join(sorted(types))}", flush=True)
    while True:
        time.sleep(15)
        with lock:
            snapshot = {listener.nvr.id: dict(listener.state) for listener in listeners}
        try:
            events.write_status(snapshot)
        except OSError as exc:
            print(f"events: cannot write status ({type(exc).__name__})", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
