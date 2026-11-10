"""Dashboard HTTPS (opt-in, DASHBOARD_TLS): pages over verified TLS, plain
http:// redirected on the same port, slow clients cannot hold up others,
fail closed without a certificate, healthcheck and deploy settings.
Uses a throw-away self-signed certificate for 127.0.0.1; no network beyond it."""
import base64
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, Request, build_opener

from nvr_track.core.paths import PROJECT_ROOT
from nvr_track.services import healthcheck
from nvr_track.web import dashboard

USER, PASSWORD = "demo", "test-password-long"
AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


def make_certificate(folder: Path) -> tuple[Path, Path]:
    cert, key = folder / "dashboard.crt", folder / "dashboard.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "2", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=test",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True, timeout=60)
    return cert, key


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to make a test certificate")
class HTTPSDashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.cert, self.key = make_certificate(self.folder)
        for target, value in (("DASHBOARD_USERNAME", USER), ("DASHBOARD_PASSWORD", PASSWORD),
                              ("LOGIN_GUARD", dashboard.LoginGuard()), ("SCAN_STATE_DIR", self.folder / "scan")):
            patcher = patch.object(dashboard, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(dashboard.TLSDashboardHandler, "tls", dashboard.tls_context(self.cert, self.key))
        patcher.start()
        self.addCleanup(patcher.stop)
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.TLSDashboardHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_port
        # The test certificate is the only trusted one: full verification, name included.
        self.client = ssl.create_default_context(cafile=str(self.cert))

    def https(self, path="/", auth=True, method="GET", headers=None, body=None):
        opener = build_opener(HTTPSHandler(context=self.client))
        hdrs = {"Authorization": AUTH} if auth else {}
        hdrs.update(headers or {})
        try:
            with opener.open(Request(f"https://127.0.0.1:{self.port}{path}", headers=hdrs, method=method,
                                     data=body), timeout=5) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            return error.code, error.headers, b""

    def test_pages_over_verified_https(self):
        self.assertEqual(self.https(auth=False)[0], 401)
        status, headers, body = self.https()
        self.assertEqual(status, 200)
        self.assertIn(b"/static/dashboard/dashboard.js", body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.https("/static/dashboard/dashboard.js")[0], 200)

    def test_plain_http_on_the_same_port_redirects(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/reports?x=1")
        response = conn.getresponse()
        self.assertEqual(response.status, 301)
        self.assertEqual(response.getheader("Location"), f"https://127.0.0.1:{self.port}/reports?x=1")
        conn.close()

    def test_plain_http_with_a_bad_host_is_refused(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            raw.sendall(b"GET / HTTP/1.1\r\nHost: evil.example\r\nX: y\r\n\r\n".replace(
                b"evil.example", b"evil.example/x\"y"))
            self.assertIn(b" 400 ", raw.recv(200))

    def test_a_silent_client_does_not_hold_up_others(self):
        with patch.object(dashboard.TLSDashboardHandler, "timeout", 0.5):
            silent = socket.create_connection(("127.0.0.1", self.port))
            self.addCleanup(silent.close)
            started = time.monotonic()
            self.assertEqual(self.https()[0], 200)              # Served while the silent one waits.
            self.assertLess(time.monotonic() - started, 3)
            silent.settimeout(5)
            self.assertEqual(silent.recv(1), b"")              # And the silent one is closed.

    def test_a_broken_handshake_leaves_the_server_working(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            raw.sendall(b"\x16\x03\x01\x00\x05garbage")
            raw.settimeout(5)
            try:
                raw.recv(100)
            except OSError:
                pass
        self.assertEqual(self.https()[0], 200)

    def test_post_origin_must_be_https(self):
        body = json.dumps({"target": "all"}).encode()
        headers = {"Content-Type": "application/json", "X-NVR-Track": "scan"}
        bad = self.https("/api/scan", method="POST", body=body,
                         headers={**headers, "Origin": f"http://127.0.0.1:{self.port}"})
        good = self.https("/api/scan", method="POST", body=body,
                          headers={**headers, "Origin": f"https://127.0.0.1:{self.port}"})
        self.assertEqual(bad[0], 403)
        self.assertNotEqual(good[0], 403)

    def test_healthcheck_speaks_https(self):
        with patch.dict(os.environ, {"DASHBOARD_TLS": "true", "DASHBOARD_PORT": str(self.port)}):
            self.assertEqual(healthcheck.dashboard(), (True, "dashboard HTTPS 401"))


class FailClosedTests(unittest.TestCase):
    def test_missing_certificate_stops_the_dashboard(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(dashboard, "DASHBOARD_USERNAME", USER), \
                patch.object(dashboard, "DASHBOARD_PASSWORD", PASSWORD), \
                patch.object(dashboard, "DASHBOARD_TLS", True), \
                patch.object(dashboard, "DASHBOARD_TLS_CERT", Path(temp) / "missing.crt"), \
                patch.object(dashboard, "DASHBOARD_TLS_KEY", Path(temp) / "missing.key"), \
                patch.object(dashboard, "REPORTS_DIR", Path(temp)), \
                patch.object(dashboard, "ThreadingHTTPServer", side_effect=AssertionError("must not listen")):
            self.assertEqual(dashboard.main(), 2)            # Never falls back to plain HTTP.


class SettingsTests(unittest.TestCase):
    def test_compose_and_git(self):
        compose = PROJECT_ROOT / "compose.yaml"
        if not compose.exists():
            self.skipTest("compose.yaml is not part of this test image")
        text = compose.read_text(encoding="utf-8")
        block = text.split("\n  nvr-dashboard:\n", 1)[1].split("\n  nvr-scanner:\n", 1)[0]
        self.assertIn('DASHBOARD_TLS: "${DASHBOARD_TLS:-false}"', block)
        self.assertIn("- ./config/tls:/app/config/tls:ro", block)
        self.assertNotIn("env_file", block)                  # Still no NVR or SMTP secrets.
        gitignore = PROJECT_ROOT / ".gitignore"
        if gitignore.exists():
            self.assertIn("config/tls/", gitignore.read_text(encoding="utf-8").splitlines())

    def test_deploy_check_and_certificate_script(self):
        script = PROJECT_ROOT / "scripts" / "deploy_check.sh"
        if not script.exists():
            self.skipTest("scripts are not part of this test image")
        text = script.read_text(encoding="utf-8")
        self.assertIn('curl -sk -o /dev/null -m 10 -w \'%{http_code}\' "https://$ADDR/"', text)
        self.assertIn("redirects to", text)
        self.assertIn("openssl x509 -checkend", text)
        cert = (PROJECT_ROOT / "scripts" / "dashboard_cert.sh").read_text(encoding="utf-8")
        self.assertIn("subjectAltName=IP:$IP", cert)
        self.assertIn('chmod 600 "$KEY.new"', cert)


if __name__ == "__main__":
    unittest.main()
