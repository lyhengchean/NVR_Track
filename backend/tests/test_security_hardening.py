"""Security hardening (Nov 2026 review): dashboard connection timeout and login
lockout, JPEG-only picture decoding, container and backup settings. No network
beyond 127.0.0.1."""
import base64
import io
import re
import socket
import threading
import time
import unittest
import warnings
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fake_nvr import jpeg

from nvr_track.web import dashboard
from nvr_track.nvr import snapshots
from nvr_track.core.paths import PROJECT_ROOT as ROOT

USER, PASSWORD = "demo", "test-password-long"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class DashboardServer(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for target, value in (("DASHBOARD_USERNAME", USER), ("DASHBOARD_PASSWORD", PASSWORD),
                              ("LOGIN_GUARD", dashboard.LoginGuard(clock=self.clock))):
            patcher = patch.object(dashboard, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_port
        return server

    def get(self, password=None, path="/api/scan"):
        headers = {}
        if password is not None:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{USER}:{password}".encode()).decode()
        try:
            with urlopen(Request(f"http://127.0.0.1:{self.port}{path}", headers=headers), timeout=5) as response:
                return response.status, dict(response.headers)
        except HTTPError as error:
            return error.code, dict(error.headers)


class ConnectionTimeoutTests(DashboardServer):
    def test_idle_connection_is_closed_without_a_login(self):
        with patch.object(dashboard.DashboardHandler, "timeout", 0.5):
            self.start()
            idle = socket.create_connection(("127.0.0.1", self.port))
            self.addCleanup(idle.close)
            idle.settimeout(5)
            started = time.monotonic()
            self.assertEqual(idle.recv(1), b"")            # Closed by the server ...
            self.assertLess(time.monotonic() - started, 4)   # ... soon, not held forever.

    def test_production_timeout_is_set(self):
        self.assertEqual(dashboard.DashboardHandler.timeout, dashboard.REQUEST_TIMEOUT_SECONDS)
        self.assertTrue(5 <= dashboard.REQUEST_TIMEOUT_SECONDS <= 120)


class LoginLockoutTests(DashboardServer):
    def setUp(self):
        super().setUp()
        self.start()

    def test_wrong_passwords_lock_the_address_out_then_expire(self):
        for n in range(dashboard.LOGIN_MAX_FAILURES):
            self.assertEqual(self.get(f"guess-{n}")[0], 401)
        code, headers = self.get(PASSWORD)                 # Locked: even the right password waits.
        self.assertEqual(code, 429)
        self.assertEqual(int(headers["Retry-After"]), dashboard.LOGIN_LOCK_SECONDS)
        self.assertEqual(self.get()[0], 429)               # No password either.
        self.clock.now += dashboard.LOGIN_LOCK_SECONDS + 1
        self.assertEqual(self.get(PASSWORD)[0], 200)

    def test_the_same_stale_password_counts_once(self):
        """An open tab polling with an old password must not lock the user out."""
        for _ in range(3 * dashboard.LOGIN_MAX_FAILURES):
            self.assertEqual(self.get("old-password-from-last-week")[0], 401)
        self.assertEqual(self.get(PASSWORD)[0], 200)

    def test_requests_without_a_password_do_not_count(self):
        for _ in range(3 * dashboard.LOGIN_MAX_FAILURES):
            self.assertEqual(self.get()[0], 401)           # Every browser's first request.
        self.assertEqual(self.get(PASSWORD)[0], 200)

    def test_old_failures_leave_the_window_and_success_resets(self):
        for n in range(dashboard.LOGIN_MAX_FAILURES - 1):
            self.get(f"guess-{n}")
        self.clock.now += dashboard.LOGIN_WINDOW_SECONDS + 1
        self.assertEqual(self.get("one-more")[0], 401)    # Window moved on: not locked.
        self.assertEqual(self.get(PASSWORD)[0], 200)
        for n in range(dashboard.LOGIN_MAX_FAILURES - 1):
            self.assertEqual(self.get(f"again-{n}")[0], 401)
        self.assertEqual(self.get(PASSWORD)[0], 200)

    def test_posts_are_guarded_too(self):
        for n in range(dashboard.LOGIN_MAX_FAILURES):
            self.get(f"guess-{n}")
        token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        request = Request(f"http://127.0.0.1:{self.port}/api/scan", data=b'{"target":"all"}', method="POST",
                          headers={"Authorization": "Basic " + token, "X-NVR-Track": "scan",
                                   "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as ctx:
            urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 429)


class LoginGuardTests(unittest.TestCase):
    def test_memory_is_bounded_and_no_password_text_is_kept(self):
        guard = dashboard.LoginGuard(clock=Clock())
        guard.MAX_ADDRESSES = 5
        for n in range(50):
            guard.failed(f"10.0.0.{n}", "Basic c2VjcmV0LXBhc3N3b3Jk")
        self.assertLessEqual(len(guard._failures), 5)
        kept = repr(guard._failures)
        self.assertNotIn("c2VjcmV0", kept)
        self.assertNotIn("secret", kept)

    def test_other_addresses_are_not_affected(self):
        guard = dashboard.LoginGuard(clock=Clock())
        for n in range(dashboard.LOGIN_MAX_FAILURES):
            guard.failed("10.0.0.9", f"guess-{n}")
        self.assertGreater(guard.locked_for("10.0.0.9"), 0)
        self.assertEqual(guard.locked_for("10.0.0.10"), 0)


class PictureDecodingTests(unittest.TestCase):
    def test_only_jpeg_is_decoded(self):
        from PIL import Image, UnidentifiedImageError
        out = io.BytesIO()
        Image.new("RGB", (32, 24), (90, 90, 90)).save(out, "PNG")
        with self.assertRaises(UnidentifiedImageError):
            snapshots.open_jpeg(out.getvalue())
        with self.assertRaises(UnidentifiedImageError):
            snapshots.analyze(out.getvalue())
        with snapshots.open_jpeg(jpeg("scene")) as image:
            self.assertEqual(image.format, "JPEG")

    def test_analysis_uses_no_deprecated_pillow_call(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            metrics = snapshots.analyze(jpeg("scene"))
            snapshots.thumbnail(jpeg("scene"))
        self.assertIn("brightness", metrics)

    def test_pillow_has_the_decoder_fixes(self):
        import PIL
        major = int(PIL.__version__.split(".")[0])
        self.assertGreaterEqual(major, 13, "Pillow below 13.0.0 has published decoder vulnerabilities")


class DeploymentSettingsTests(unittest.TestCase):
    def test_every_container_drops_privilege_gain(self):
        path = ROOT / "compose.yaml"
        if not path.exists():
            self.skipTest("compose.yaml is not part of this test image")
        services = re.split(r"\n  (?=nvr-[a-z-]+:\n)", path.read_text(encoding="utf-8"))[1:]
        self.assertEqual(len(services), 7)
        for block in services:
            name = block.split(":", 1)[0]
            self.assertIn("no-new-privileges:true", block, name)
            self.assertIn("cap_drop:\n      - ALL", block, name)
            self.assertNotRegex(block, r"(?m)^    (user|privileged|cap_add):", name)

    def test_backup_and_log_are_private_from_the_start(self):
        script = (ROOT / "scripts" / "deploy_check.sh").read_text(encoding="utf-8")
        self.assertIn('(umask 077 && tar --exclude=./reports -czf "$NEW"', script)   # Then moved into place.
        self.assertIn('(umask 077 && : > "$LOG")', script)
        self.assertNotRegex(script, r"(?m)^umask ")       # Pulled source files keep normal modes.


if __name__ == "__main__":
    unittest.main()
