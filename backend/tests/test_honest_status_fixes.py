"""Honest status after known issues, alerts that must not falsely resolve, the
email's camera list, real-time event delivery and the scanner's timeout."""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fake_nvr import event_xml
from test_telegram_bot import Fixture, channel

from nvr_track.web import dashboard
from nvr_track.nvr import events
from nvr_track.monitoring import notes
from nvr_track.bot import telegram_views as views
from nvr_track.monitoring.camera_condition import classify, retention_shortfall
from nvr_track.services import event_listener, scan_worker
from nvr_track.cli.run_all_nvr_reports import camera_conditions, send_email_report
from nvr_track.core.paths import PROJECT_ROOT
from frontend_source import DASHBOARD_UI


class NotesFixture(Fixture):
    def setUp(self):
        super().setUp()
        for name in ("notes", "events"):
            (self.root / name).mkdir()
        env = patch.dict("os.environ", {"NOTES_DIR": str(self.root / "notes"),
                                        "EVENTS_DIR": str(self.root / "events")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("NVR_REQUIRED_RETENTION_DAYS", None)

    def alerts(self):
        self.calls.clear()
        self.bot.check_alerts()
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


class KnownIssueClaimTests(NotesFixture):
    def test_known_issue_camera_without_footage_is_never_called_recording(self):
        self.report(channels=[channel(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE", days=0)])
        notes.add_known_issue("nvr-01", "2", "Camera removed", None, "Heng")
        data = dashboard.read_dashboard_data()
        self.assertEqual(data["nvrs"][0]["condition_counts"]["KNOWN_ISSUE"], 1)
        self.assertEqual(data["summary"]["known_not_recording"], 1)
        status = views.fleet_status(data)
        self.assertNotIn("No monitored camera has stopped recording", status)
        self.assertIn("No camera needs action", status)
        self.assertIn("1 camera with a known issue is not recording", status)
        # The web page uses the same count and checks every camera before "All cameras recorded".
        self.assertIn("s.known_not_recording", DASHBOARD_UI)
        self.assertIn("recorded === nvr.channels.length", DASHBOARD_UI)

    def test_known_issue_on_a_recording_camera_is_not_counted(self):
        black = {**channel(1), "snapshot_result": {"status": "PICTURE_BLACK"}}
        self.report(channels=[black, channel(2)])
        notes.add_known_issue("nvr-01", "1", "Lens covered by builders", None, "Heng")
        data = dashboard.read_dashboard_data()
        self.assertEqual(data["nvrs"][0]["channels"][0]["condition"]["code"], "KNOWN_ISSUE")
        self.assertEqual(data["summary"]["known_not_recording"], 0)
        self.assertIn("No monitored camera has stopped recording", views.fleet_status(data))


class NoFalseResolvedTests(NotesFixture):
    def gap(self, days):
        cam = channel(1, days=days)
        cam["retention_result"]["calendar"][-3]["has_recording"] = False
        cam["retention_result"]["days_with_footage"] = days - 1
        return cam

    def test_below_required_days_is_not_resolved_by_a_gap_or_offline_state(self):
        with patch.dict("os.environ", {"NVR_REQUIRED_RETENTION_DAYS": "20"}):
            self.report(channels=[channel(1)])
            self.alerts()                                                 # Baseline.
            self.report(channels=[channel(1, days=10)])
            self.assertIn("Below required days", self.alerts()[0])
            for cam, code in ((self.gap(10), "GAPS"),
                              ({**channel(1, days=10), "reported_status": "OFFLINE"}, "OFFLINE_RECORDING")):
                self.assertEqual(classify(cam)["code"], code)
                self.assertTrue(retention_shortfall(cam)["below"])
                self.report(channels=[cam])
                self.assertTrue(all("Keeps the required days again" not in t for t in self.alerts()), code)
            self.report(channels=[channel(1, days=25)])
            self.assertIn("Keeps the required days again", self.alerts()[0])

    def test_switching_the_rule_off_drops_the_alert_without_a_message(self):
        with patch.dict("os.environ", {"NVR_REQUIRED_RETENTION_DAYS": "20"}):
            self.report(channels=[channel(1)])
            self.alerts()
            self.report(channels=[channel(1, days=10)])
            self.assertIn("Below required days", self.alerts()[0])
        self.assertEqual(self.alerts(), [])
        state = json.loads((self.root / "tg" / "alerts.json").read_text(encoding="utf-8"))["state"]
        self.assertFalse(any(key.endswith(":ret") for key in state))

    def test_quiet_event_camera_is_not_reported_as_recording_again(self):
        offline = {**channel(2, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE"), "recording_expectation": "event"}
        self.report(channels=[channel(1), offline])
        self.alerts()                                                     # Baseline: ch2 offline.
        # A "Stopped recording" still open from before (offline cameras no longer get one).
        path = self.bot.state_dir / "alerts.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["state"]["cam:nvr-01:2:rec"] = {"severity": "critical", "text": "Stopped recording: ch2",
                                              "resolved": "✅ Recording again: ch2", "value": ""}
        path.write_text(json.dumps(saved), encoding="utf-8")
        quiet = {**channel(2, "NO_RECENT_RECORDING_EVIDENCE"), "recording_expectation": "event"}
        self.assertEqual(classify(quiet)["code"], "NO_RECENT_EVENTS")
        self.report(channels=[channel(1), quiet])
        sent = self.alerts()
        self.assertTrue(any("Back online" in t for t in sent))
        self.assertTrue(all("Recording again" not in t for t in sent))
        self.report(channels=[channel(1), {**channel(2), "recording_expectation": "event"}])
        self.assertIn("Recording again", self.alerts()[0])


class EmailCameraListTests(unittest.TestCase):
    @patch("nvr_track.cli.run_all_nvr_reports.smtplib.SMTP")
    def test_failed_checks_and_offline_recording_cameras_are_listed(self, smtp):
        report = {"nvr": {"id": "nvr-01"}, "channel_inventory": {"channels": [
            channel(1), channel(2, "TRACK_MAPPING_UNVERIFIED"), channel(3, live="OFFLINE")]}}
        result = camera_conditions(report)
        self.assertEqual((result["check_failed"], result["offline_recording"]), (1, 1))
        self.assertEqual(sorted(a["condition"] for a in result["attention"]),
                         ["Check failed", "Offline, still recording"])
        env = {"SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p",
               "SMTP_FROM": "a@example.com", "SMTP_TO": "b@example.com"}
        row = {"id": "nvr-01", "ip": "192.0.2.1", "overall": "WARNING", "storage": "HEALTHY", **result}
        with patch.dict("os.environ", env):
            send_email_report([row])
        message = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        html = message.get_body(("html",)).get_content()
        text = message.get_body(("plain",)).get_content()
        self.assertIn("Checks failed or unverified</td><td>1", html)
        self.assertIn("Check failed: nvr-01 ch2", text)
        self.assertIn("Offline, still recording: nvr-01 ch3", text)


class StorageFindingTests(unittest.TestCase):
    def test_email_findings_say_which_disk(self):
        report = {"nvr": {"id": "nvr-04"}, "channel_inventory": {"channels": [channel(1)]},
                  "storage": {"status": "CRITICAL", "message": "NVR reports abnormal HDD states: 3 (error)."}}
        self.assertEqual(camera_conditions(report)["findings"][0],
                         "Storage critical: NVR reports abnormal HDD states: 3 (error).")
        report["storage"]["status"] = "HEALTHY"
        self.assertEqual(camera_conditions(report)["findings"], [])

    def test_reporter_prints_every_warning_uncut(self):
        from nvr_track.cli.run_nvr_report import reason_lines
        storage = ("Storage warning: HDD slots reported not detected (2, 9); confirm whether drives are expected; "
                   "HDDs 1, 3, 4, 5 are full, but cameras recorded within the last 60 minutes, so old footage is "
                   "being overwritten.")
        overall = {"critical_reasons": [], "warnings": [
            "3 channels are reported offline.", storage,
            "3 continuous-recording cameras have no evidence in the configured window.",
            "1 cameras have a picture problem (black, covered, frozen or moved)."]}
        lines = reason_lines(overall)
        self.assertEqual(len(lines), 4)                                   # The 4th (picture) was hidden before.
        self.assertIn("Warning: " + storage, lines)                      # Not cut at 160 characters.
        self.assertTrue(lines[-1].startswith("Warning: 1 cameras have a picture problem"))

    def test_deploy_check_shows_all_reasons(self):
        script = PROJECT_ROOT / "scripts" / "deploy_check.sh"
        if not script.exists():
            self.skipTest("deploy_check.sh is not part of this test image")
        text = script.read_text(encoding="utf-8")
        self.assertIn("s/^Warning: /            warning: /p' | head -10", text)
        self.assertIn("s/^Critical: /            critical: /p' | head -10", text)


class StreamServer:
    """One HTTP client: headers, optional documents, then holds the stream open."""

    def __init__(self, documents=()):
        self.release = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.documents = documents
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        conn, _ = self.sock.accept()
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: multipart/mixed; boundary=b\r\n"
                         b"Connection: close\r\n\r\n")
            for document in self.documents:
                conn.sendall(b"--b\r\nContent-Type: application/xml\r\n\r\n" + document.encode() + b"\r\n")
            self.release.wait(5)
        self.sock.close()


class EventDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict("os.environ", {"EVENTS_DIR": temp.name, "NVR_USERNAME": "admin", "NVR_PASSWORD": "x"})
        env.start()
        self.addCleanup(env.stop)

    def listener(self, port):
        class Inventory:
            id, ip, http_port = "nvr-01", "127.0.0.1", port
        return event_listener.Listener(Inventory, events.wanted_types(), events.Deduplicator(30), threading.Lock())

    def test_event_is_saved_while_the_stream_stays_open(self):
        server = StreamServer([event_xml("videoloss", "2")])
        listener = self.listener(server.port)
        outcome = {}

        class Stop(Exception):
            pass

        def stop(_seconds):
            raise Stop

        def run():
            try:
                listener.run()
            except Stop:
                outcome["stopped"] = True
        with patch.object(event_listener.time, "sleep", stop), patch("sys.stdout", new_callable=io.StringIO):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 3
            seen = False
            while not seen and time.monotonic() < deadline:
                seen = bool(events.recent_events())
                server.release.wait(0.05)
            server.release.set()                                          # Only now does the stream close.
            thread.join(5)
        self.assertTrue(seen, "the event waited for the stream to close")
        self.assertEqual([(e["type"], e["channel"]) for e in events.recent_events()], [("videoloss", "2")])
        self.assertTrue(outcome.get("stopped"))
        self.assertEqual(listener.state["last_error"], "STREAM_CLOSED")

    def test_silent_stream_times_out_as_a_disconnect(self):
        server = StreamServer()
        self.addCleanup(server.release.set)
        listener = self.listener(server.port)

        class Stop(Exception):
            pass

        def stop(_seconds):
            raise Stop
        with patch.object(event_listener, "READ_TIMEOUT", 0.3), patch.object(event_listener.time, "sleep", stop), \
                patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(Stop):                                 # Reconnect wait, not a crash.
                listener.run()
        self.assertFalse(listener.state["connected"])
        self.assertEqual(listener.state["last_error"], "NO_DATA_TIMEOUT")


class BotEventStartTests(NotesFixture):
    def test_first_start_skips_every_old_event_even_in_a_large_file(self):
        self.report(channels=[channel(1)])
        line = {"nvr": "nvr-01", "type": "videoloss", "label": "Video loss", "severity": "critical",
                "channel": "1", "at": datetime.now(timezone.utc).isoformat(), "device_time": "",
                "description": "x" * 150}
        text = (json.dumps(line) + "\n") * 12000                          # About 3.5 MB, over one 2 MB read.
        (self.root / "events" / "events.jsonl").write_text(text, encoding="utf-8")
        self.calls.clear()
        for _ in range(3):
            self.bot.check_events()
        self.assertEqual([m for m, _ in self.calls if m == "sendMessage"], [])
        events.record("nvr-01", {"type": "videoloss", "state": "active", "channel": "1", "count": 1,
                                 "device_time": "", "description": ""}, events.EVENT_TYPES)
        self.bot.check_events()
        sent = [p["text"] for m, p in self.calls if m == "sendMessage"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].count("Video loss"), 1)

    def test_end_position_stops_before_a_half_written_line(self):
        path = self.root / "events" / "events.jsonl"
        path.write_bytes(b'{"a": 1}\n{"b": ')
        self.assertEqual(events.end_position()["offset"], len(b'{"a": 1}\n'))
        self.assertEqual(events.end_position(self.root / "missing"), {})


@unittest.skipUnless(sys.platform.startswith("linux"), "process groups and /proc")
class ScannerTimeoutTests(unittest.TestCase):
    def test_timeout_also_stops_the_reporter_the_runner_started(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        pid_file = Path(temp.name) / "reporter.pid"
        code = ("import subprocess, sys, time\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
                " stdout=subprocess.DEVNULL)\n"
                f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
                "print('Starting nvr-01 at 127.0.0.1:80', flush=True)\n"
                "time.sleep(30)\n")
        job = {"id": "t", "target": "nvr-01", "trigger": "manual", "email": False, "requested_at": None}
        with patch.object(scan_worker, "STATE_DIR", Path(temp.name)), \
                patch.object(scan_worker, "command_for", return_value=[sys.executable, "-c", code]), \
                patch.object(scan_worker, "report_timeout_seconds", return_value=0), \
                patch.object(scan_worker, "RUN_TIMEOUT_MARGIN_SECONDS", 2), \
                patch("sys.stdout", new_callable=io.StringIO):
            scan_worker.Service().run(job, [])
        status = json.loads((Path(temp.name) / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["last"]["result"], "TIMEOUT")
        reporter = int(pid_file.read_text())

        def alive() -> bool:
            try:
                state = Path(f"/proc/{reporter}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except OSError:
                return False
            return state != "Z"
        deadline = time.monotonic() + 3
        while alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(), "the per-NVR reporter kept running after the timeout")


if __name__ == "__main__":
    unittest.main()
