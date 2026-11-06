"""Docker healthchecks (scripts/healthcheck.py), the heartbeats behind them,
and the pinned urllib3. Local only, no NVR."""
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts import event_listener, healthcheck
from scripts import telegram_bot as tb

ROOT = Path(__file__).resolve().parent.parent


class Stop(Exception):
    pass


def server(code: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class DashboardCheckTests(unittest.TestCase):
    def check(self, code, **env):
        httpd = server(code)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with patch.dict("os.environ", {"DASHBOARD_PORT": str(httpd.server_port), **env}):
            return healthcheck.dashboard()

    def test_login_page_is_healthy(self):
        self.assertEqual(self.check(401), (True, "dashboard HTTP 401"))
        self.assertTrue(self.check(200)[0])
        # A proxy setting must not catch the local request.
        self.assertTrue(self.check(401, HTTP_PROXY="http://127.0.0.1:9", http_proxy="http://127.0.0.1:9")[0])

    def test_server_error_or_no_server_is_unhealthy(self):
        self.assertEqual(self.check(500), (False, "dashboard HTTP 500"))
        httpd = server(401)
        port = httpd.server_port
        httpd.shutdown()
        httpd.server_close()
        with patch.dict("os.environ", {"DASHBOARD_PORT": str(port)}):
            healthy, text = healthcheck.dashboard()
        self.assertFalse(healthy)
        self.assertIn("not answering", text)
        with patch.dict("os.environ", {"DASHBOARD_PORT": "80; rm"}):
            self.assertFalse(healthcheck.dashboard()[0])


class HeartbeatCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "heartbeat"

    def test_age_limits(self):
        self.assertEqual(healthcheck.heartbeat(str(self.path), "120"), (False, "no heartbeat file heartbeat"))
        self.path.touch()
        self.assertTrue(healthcheck.heartbeat(str(self.path), "120")[0])
        old = time.time() - 700
        os.utime(self.path, (old, old))
        healthy, text = healthcheck.heartbeat(str(self.path), "600")
        self.assertFalse(healthy)
        self.assertIn("(limit 600 s)", text)
        self.assertFalse(healthcheck.heartbeat(str(self.path), "x")[0])

    def test_exit_codes(self):
        self.path.touch()
        self.assertEqual(healthcheck.main(["heartbeat", str(self.path), "60"]), 0)
        self.assertEqual(healthcheck.main(["heartbeat", str(self.path) + ".missing", "60"]), 1)
        self.assertEqual(healthcheck.main(["nonsense"]), 2)
        self.assertEqual(healthcheck.main([]), 2)


class HeartbeatWriterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_bot_beats_while_working_and_while_idle(self):
        tb.beat(self.root / "tg")
        self.assertTrue((self.root / "tg" / "heartbeat").is_file())
        with patch.object(tb.time, "sleep", side_effect=Stop):
            with self.assertRaises(Stop):
                tb.idle("No token.", self.root / "idle")
        self.assertTrue((self.root / "idle" / "heartbeat").is_file())
        tb.beat(Path("/proc/no-such-dir"))                                     # Never raises.

    def test_idle_event_listener_keeps_its_status_fresh(self):
        calls = []

        def sleep(seconds):
            calls.append(seconds)
            if len(calls) == 2:
                raise Stop
            os.utime(self.root / "status.json", (time.time() - 500, time.time() - 500))
        with patch.dict("os.environ", {"NVR_EVENT_NVR_IDS": "", "EVENTS_DIR": str(self.root)}), \
                patch.object(event_listener, "load_inventory", return_value=[]), \
                patch.object(event_listener.time, "sleep", side_effect=sleep):
            with self.assertRaises(Stop):
                event_listener.main()
        self.assertEqual(calls, [60, 60])
        self.assertTrue(healthcheck.heartbeat(str(self.root / "status.json"), "180")[0])  # Rewritten.


class ComposeTests(unittest.TestCase):
    def setUp(self):
        if not (ROOT / "compose.yaml").exists():
            self.skipTest("compose.yaml is not part of this test image")
        self.text = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    def block(self, name):
        return self.text.split(f"  {name}:", 1)[1].split("\n  nvr-", 1)[0]

    def test_every_long_running_service_has_a_local_healthcheck(self):
        for name in ("nvr-dashboard", "nvr-scanner", "nvr-telegram", "nvr-events", "nvr-live"):
            block = self.block(name)
            self.assertIn('"scripts.healthcheck"', block, name)
            self.assertIn("restart: unless-stopped", block, name)
            beat = re.search(r'"heartbeat", "(/app/[^"]+)", "(\d+)"', block)
            if beat:
                folder = str(Path(beat[1]).parent)
                mounts = re.findall(r"- \./[^:]+:(/app/[^:\s]+)(:ro)?", block)
                self.assertIn((folder, ""), mounts, f"{name}: heartbeat folder must be a writable mount")
        for name in ("nvr-report", "nvr-report-all"):                         # One-shot runs.
            self.assertNotIn("healthcheck", self.text.split(f"  {name}:", 1)[1].split("\n  nvr-", 1)[0])


class PinnedUrllib3Tests(unittest.TestCase):
    def test_pinned_and_installed_versions_match(self):
        import urllib3
        from urllib3.response import HTTPResponse
        pins = dict(re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", (ROOT / "requirements.txt").read_text(), re.M))
        self.assertEqual(pins.get("urllib3"), urllib3.__version__)
        self.assertTrue(hasattr(HTTPResponse, "read1"))                       # Used by the event listener.


if __name__ == "__main__":
    unittest.main()
