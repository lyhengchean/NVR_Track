"""Probe: a new NVR login tried on one NVR (full read-only scan into a temporary
folder, refusals named from the NVR's own 401/403 answers) and HTTPS to the NVR
(certificate fingerprint, pinned GETs). End-to-end against the fake NVR."""
import hashlib
import io
import json
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests

from fake_nvr import FakeNVR

from nvr_track.cli import probe_nvr_features as probe
from nvr_track.core.config import NVRConfig, load_inventory


class CheckNameTests(unittest.TestCase):
    def test_requests_are_named_in_plain_words(self):
        self.assertEqual(probe.check_name("POST", "/ISAPI/ContentMgmt/search"), "recording search (playback right)")
        self.assertEqual(probe.check_name("GET", "/ISAPI/ContentMgmt/Storage/hdd/2/SMARTTest/status"), "disk SMART")
        self.assertEqual(probe.check_name("GET", "/ISAPI/Streaming/channels/101/picture"),
                         "camera pictures (live view right)")
        self.assertEqual(probe.check_name("GET", "/ISAPI/ContentMgmt/InputProxy/channels/status"),
                         "camera online status")
        self.assertEqual(probe.check_name("POST", "/ISAPI/ContentMgmt/record/tracks/101/dailyDistribution"),
                         "30-day recording calendar (playback right)")
        self.assertEqual(probe.check_name("GET", "/ISAPI/Other/12/thing"), "GET /ISAPI/Other/<n>/thing")


class LoginProbeTests(unittest.TestCase):
    """The scan runs as a real child process against the fake NVR."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "reports").mkdir()
        self.nvr = FakeNVR()
        self.addCleanup(self.nvr.close)
        inventory = self.root / "nvrs.local.json"
        inventory.write_text(json.dumps({"schema_version": 1, "nvrs": [
            {"id": "nvr-01", "name": "Kiln", "ip": "127.0.0.1", "http_port": self.nvr.port,
             "expected_channels": 4}]}))
        self.config = load_inventory(inventory)[0]
        for patcher in (patch.object(probe, "ROOT", self.root),
                        patch.object(probe, "INVENTORY_PATH", inventory),
                        patch.dict("os.environ", {
                            "NVR_INVENTORY_PATH": str(inventory),
                            "NVR_USERNAME": "admin", "NVR_PASSWORD": "env-password",
                            "NVR_PER_CAMERA_RECORDING_IDS": "nvr-01", "NVR_RECORDING_RETRY_DELAY_SECONDS": "0",
                            "NOTES_DIR": str(self.root / "notes"), "EVENTS_DIR": str(self.root / "events")})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def scan(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = probe.scan_with_login(self.config, "viewer", "typed-password")
        return code, out.getvalue()

    def test_a_login_with_all_rights_passes_and_nothing_is_kept(self):
        calls = []
        real_run = subprocess.run

        def run(command, **kwargs):
            calls.append((command, kwargs["env"]))
            return real_run(command, **kwargs)

        with patch.object(probe.subprocess, "run", run):
            code, out = self.scan()
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing refused and nothing left unverified.", out)
        command, env = calls[0]
        self.assertEqual(command[1:3], ["-m", "nvr_track.cli.probe_login_scan"])
        self.assertEqual((env["NVR_01_USERNAME"], env["NVR_01_PASSWORD"]), ("viewer", "typed-password"))
        self.assertEqual(env["NVR_PASSWORD"], "env-password")            # .env login untouched.
        report_dir = Path(command[command.index("--report-dir") + 1])
        self.assertFalse(report_dir.exists())                             # Temporary folder removed.
        self.assertEqual(list((self.root / "reports").iterdir()), [])     # Nothing saved in reports/.
        self.assertNotIn("typed-password", out)
        # Read-only: the only POSTs are recording searches.
        self.assertTrue(all(p == "/ISAPI/ContentMgmt/search" for m, p in self.nvr.requests if m == "POST"))

    def test_refused_checks_are_named_and_fail(self):
        self.nvr.forbidden = {"/ISAPI/ContentMgmt/search", "/ISAPI/ContentMgmt/Storage/hdd"}
        code, out = self.scan()
        self.assertEqual(code, 1, out)
        self.assertIn("Refused by the NVR for this login:", out)
        self.assertIn("- disks and free space: HTTP 403 x1", out)
        self.assertIn("- recording search (playback right): HTTP 403 x1", out)
        self.assertNotIn("Nothing refused", out)
        self.assertNotIn("typed-password", out)

    def test_unverified_checks_are_not_success(self):
        self.nvr.search_replies["2"] = "<not-xml"
        code, out = self.scan()
        self.assertEqual(code, 1, out)
        self.assertIn("Nothing refused, but these checks did not complete in this scan:", out)
        self.assertIn("- 1 per-camera recording checks were unverified or not run.", out)
        # The same gap in the last saved scan (current login) is not blamed on the new login.
        (self.root / "reports" / "nvr-01_20261110T080000+0700.json").write_text(json.dumps(
            {"overall": {"unknown_checks": ["1 per-camera recording checks were unverified or not run."]}}))
        code, out = self.scan()
        self.assertEqual(code, 0, out)
        self.assertIn("Unverified here as in the last saved scan (not caused by this login)", out)

    def test_a_scan_that_writes_no_report_is_not_success(self):
        with patch.object(probe.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 2, stdout="Cannot load local NVR inventory\n", stderr="")):
            code, out = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("Not verified: no report written (exit code 2)", out)

    def test_a_wrong_login_stops_after_one_request(self):
        self.nvr.refuse_login = True
        out = io.StringIO()
        with patch.object(sys, "argv", ["probe", "--nvr-id", "nvr-01", "--ask-login", "--https"]), \
                patch.object(probe, "ask_login", return_value=("viewer", "wrong-password")), redirect_stdout(out):
            code = probe.main()
        self.assertEqual(code, 1)
        self.assertIn("Login refused (HTTP 401)", out.getvalue())
        self.assertIn(".env is not changed", out.getvalue())
        self.assertEqual(self.nvr.requests, [("GET", "/ISAPI/System/status")])

    def test_ask_login_needs_a_terminal(self):
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("viewer\n")), redirect_stdout(out):
            self.assertIsNone(probe.ask_login())
        self.assertIn("WITHOUT -T", out.getvalue())


def make_certificate(folder: Path) -> tuple[Path, Path]:
    cert, key = folder / "nvr.crt", folder / "nvr.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "2", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=fake-nvr",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True, timeout=60)
    return cert, key


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to make a test certificate")
class HTTPSProbeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cert, key = make_certificate(Path(temp.name))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.nvr = FakeNVR(tls=context)
        self.addCleanup(self.nvr.close)
        der = ssl.PEM_cert_to_DER_cert(cert.read_text())
        self.fingerprint = hashlib.sha256(der).hexdigest()
        self.config = NVRConfig(id="nvr-01", ip="127.0.0.1", expected_channels=4)

    def check(self, port):
        out = io.StringIO()
        with redirect_stdout(out):
            code = probe.https_check(self.config, port, "admin", "pw")
        return code, out.getvalue()

    def test_certificate_is_shown_and_pinned_gets_work(self):
        code, out = self.check(self.nvr.port)
        self.assertEqual(code, 0, out)
        pretty = ":".join(self.fingerprint[i:i + 2] for i in range(0, 64, 2)).upper()
        self.assertIn(f"certificate SHA-256 {pretty}", out)
        self.assertIn("subject CN=fake-nvr", out)
        self.assertIn("GET /ISAPI/System/deviceInfo: HTTP 200", out)
        self.assertIn("GET /ISAPI/ContentMgmt/InputProxy/channels/status: HTTP 200", out)
        self.assertIn("HTTPS works with this certificate", out)
        self.assertEqual({m for m, _ in self.nvr.requests}, {"GET"})

    def test_the_pin_refuses_any_other_certificate(self):
        with requests.Session() as session:
            session.trust_env, session.verify = False, False
            session.mount("https://", probe.PinnedAdapter(self.fingerprint))
            self.assertEqual(session.get(f"https://127.0.0.1:{self.nvr.port}/ISAPI/System/deviceInfo",
                                         timeout=5).status_code, 200)
        with requests.Session() as session:
            session.trust_env, session.verify = False, False
            session.mount("https://", probe.PinnedAdapter("00" * 32))
            with self.assertRaises(requests.exceptions.SSLError):
                session.get(f"https://127.0.0.1:{self.nvr.port}/ISAPI/System/deviceInfo", timeout=5)

    def test_refused_https_requests_are_not_success(self):
        self.nvr.forbidden = {"/ISAPI/System/deviceInfo"}
        code, out = self.check(self.nvr.port)
        self.assertEqual(code, 1)
        self.assertIn("GET /ISAPI/System/deviceInfo: HTTP 403", out)
        self.assertIn("HTTPS is not usable for the checks yet.", out)

    def test_no_https_port(self):
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            port = spare.getsockname()[1]
        code, out = self.check(port)
        self.assertEqual(code, 1)
        self.assertIn("not usable", out)


if __name__ == "__main__":
    unittest.main()
