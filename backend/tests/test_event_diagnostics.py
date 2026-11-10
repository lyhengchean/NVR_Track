"""Real-time event streams: a readable reason when a stream cannot connect
(nvr-01, 06 Nov: only "ConnectionError"), and no new post when an NVR
re-sends a still-active alarm on reconnect (nvr-12 "Disk error" at every
restart). Local sockets only."""
import json
import socket
import tempfile
import threading
import threading as _threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from requests.auth import HTTPDigestAuth

from test_telegram_bot import Fixture, channel

from nvr_track.nvr import events
from nvr_track.services import event_listener
from nvr_track.core.paths import PROJECT_ROOT as ROOT
from frontend_source import DASHBOARD_UI


class Stop(Exception):
    pass


def closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def hang_up_server() -> tuple[int, socket.socket]:
    """Accepts, reads the request, closes without an answer (like a full NVR)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.recv(4096)
            conn.close()
    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], server


def failure(url: str) -> BaseException:
    try:
        requests.get(url, auth=HTTPDigestAuth("u", "p"), timeout=(2, 2))
    except requests.RequestException as exc:
        return exc
    raise AssertionError("expected a failure")


class ReasonTests(unittest.TestCase):
    def test_real_failures(self):
        self.assertEqual(events.reason(failure(f"http://127.0.0.1:{closed_port()}/x")), "REFUSED")
        port, server = hang_up_server()
        self.addCleanup(server.close)
        self.assertEqual(events.reason(failure(f"http://127.0.0.1:{port}/x")), "CLOSED_BY_NVR")

    def test_codes_and_texts(self):
        self.assertEqual(events.reason(ConnectionError("HTTP_401")), "HTTP_401")
        self.assertEqual(events.reason(ConnectionError("STREAM_CLOSED")), "STREAM_CLOSED")
        self.assertEqual(events.reason(requests.ConnectTimeout()), "CONNECT_TIMEOUT")
        self.assertEqual(events.reason(requests.exceptions.ChunkedEncodingError()), "STREAM_BROKEN")
        inner = ConnectionResetError(104, "Connection reset by peer")
        from urllib3.exceptions import ProtocolError
        self.assertEqual(events.reason(requests.ConnectionError(ProtocolError("Connection aborted.", inner))),
                         "RESET_BY_NVR")
        self.assertEqual(events.reason(ValueError("x")), "ValueError")
        self.assertIn("maximum number of event clients", events.reason_text("CLOSED_BY_NVR"))
        self.assertEqual(events.reason_text("HTTP_503"), "HTTP error (HTTP_503)")
        self.assertEqual(events.reason_text(None), "no reply")

    def test_listener_records_the_reason(self):
        nvr = SimpleNamespace(id="nvr-01", ip="127.0.0.1", http_port=closed_port())
        listener = event_listener.Listener(nvr, events.EVENT_TYPES, events.Deduplicator(), _threading.Lock())
        with patch.dict("os.environ", {"NVR_USERNAME": "u", "NVR_PASSWORD": "p"}), \
                patch.object(event_listener.time, "sleep", side_effect=Stop), \
                patch("builtins.print") as out:
            with self.assertRaises(Stop):
                listener.run()
        self.assertEqual((listener.state["connected"], listener.state["last_error"]), (False, "REFUSED"))
        self.assertIn("nvr-01 cannot connect (REFUSED); retrying", out.call_args[0][0])


class ReplayTests(unittest.TestCase):
    T = datetime(2026, 11, 6, 4, 13, 12, tzinfo=timezone.utc)

    def event(self, kind="diskerror", channel="1"):
        return {"type": kind, "channel": channel, "state": "active", "device_time": "", "description": ""}

    def test_an_alarm_replayed_on_connect_is_posted_once(self):
        seen = [{"nvr": "nvr-12", "type": "diskerror", "channel": "", "at": (self.T - timedelta(hours=1)).isoformat()}]
        dedupe = events.Deduplicator(30, seen=seen)                           # Restart: log remembered.
        nvr_level = {**self.event(), "channel": ""}
        self.assertFalse(dedupe.accept("nvr-12", nvr_level, now=self.T, replay=True))
        self.assertFalse(dedupe.accept("nvr-12", nvr_level, now=self.T + timedelta(hours=20), replay=True))
        self.assertTrue(dedupe.accept("nvr-12", nvr_level, now=self.T + timedelta(hours=21)))  # A new one, live.
        self.assertTrue(dedupe.accept("nvr-12", {**self.event("hdderror"), "channel": ""}, now=self.T, replay=True))
        old = events.Deduplicator(30, seen=[{**seen[0], "at": (self.T - timedelta(hours=25)).isoformat()}])
        self.assertTrue(old.accept("nvr-12", nvr_level, now=self.T, replay=True))  # Once a day at most.

    def test_listener_keeps_replays_out_of_the_log(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"EVENTS_DIR": tmp}), \
                patch("builtins.print"):
            nvr = SimpleNamespace(id="nvr-12", ip="127.0.0.1", http_port=1)
            listener = event_listener.Listener(nvr, events.EVENT_TYPES, events.Deduplicator(30), _threading.Lock())
            listener.handle(self.event(channel="0"), replay=True)              # First time: recorded.
            restarted = event_listener.Listener(nvr, events.EVENT_TYPES,
                                                events.Deduplicator(30, seen=events.recent_events(24)),
                                                _threading.Lock())
            restarted.handle(self.event(channel="0"), replay=True)             # Replayed after a restart.
            lines = (Path(tmp) / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["channel"], "")
        self.assertEqual(restarted.state["replayed"], 1)


class ShownTests(Fixture):
    def test_telegram_and_dashboard_name_the_reason(self):
        (self.root / "events").mkdir(exist_ok=True)
        env = patch.dict("os.environ", {"EVENTS_DIR": str(self.root / "events")})
        env.start()
        self.addCleanup(env.stop)
        self.report(channels=[channel(1)])
        self.bot.check_alerts()                                                # First start: baseline.
        (self.root / "events" / "status.json").write_text(json.dumps({
            "schema_version": 1, "updated_at": datetime.now(timezone.utc).isoformat(),
            "nvrs": {"nvr-01": {"connected": False, "last_error": "CLOSED_BY_NVR"}}}))
        self.calls.clear()
        self.bot.check_alerts()
        texts = [p["text"] for m, p in self.calls if m == "sendMessage"]
        self.assertTrue(any("event stream disconnected (closed by the NVR (it may already serve" in t for t in texts))
        from nvr_track.web import dashboard
        stream = dashboard.read_dashboard_data()["nvrs"][0]["event_stream"]
        self.assertEqual((stream["last_error"], stream["last_error_text"][:20]), ("CLOSED_BY_NVR", "closed by the NVR (i"))
        self.assertIn("st.last_error_text", DASHBOARD_UI)

    def test_deploy_check_reports_each_stream(self):
        text = (ROOT / "scripts" / "deploy_check.sh").read_text(encoding="utf-8")
        self.assertIn("Real-time event streams connected: $UP of $TOTAL", text)
        self.assertIn("real-time event stream not connected: $WHY", text)


if __name__ == "__main__":
    unittest.main()
