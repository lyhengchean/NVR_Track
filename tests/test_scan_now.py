"""Scheduled scans, the dashboard's scan requests, and the scanner's safety checks."""
import base64
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import dashboard
from scripts import scan_worker as worker
from scripts import run_all_nvr_reports as fleet

ICT = timezone(timedelta(hours=7))
EVERY_2H = worker.parse_times("06:00,08:00,10:00,12:00,14:00,16:00,18:00,20:00,22:00", "08:00")


def at(hour, minute=0, day=2):
    return datetime(2026, 11, day, hour, minute, tzinfo=ICT)


class ScheduleTests(unittest.TestCase):
    def test_times_are_parsed_sorted_and_invalid_ones_ignored(self):
        self.assertEqual(worker.parse_times("22:00, 06:00,25:00,8:00,06:00", "08:00"), [(6, 0), (22, 0)])
        self.assertEqual(worker.parse_times("", "08:00"), [(8, 0)])
        self.assertEqual(len(EVERY_2H), 9)

    def test_due_slot_runs_once_per_slot(self):
        self.assertEqual(worker.due_slot(at(10, 1), EVERY_2H, at(8)), at(10))
        self.assertIsNone(worker.due_slot(at(10, 5), EVERY_2H, at(10)))
        self.assertIsNone(worker.due_slot(at(11, 0), EVERY_2H, at(10)))

    def test_old_missed_slot_is_skipped_but_short_restart_catches_up(self):
        self.assertEqual(worker.due_slot(at(8, 20), EVERY_2H, at(6)), at(8))
        self.assertIsNone(worker.due_slot(at(9, 0), EVERY_2H, at(6)))

    def test_night_gap_and_next_slot(self):
        self.assertIsNone(worker.due_slot(at(3), EVERY_2H, at(22, day=1)))
        self.assertEqual(worker.next_slot(at(22, 30), EVERY_2H), at(6, day=3))
        self.assertEqual(worker.latest_slot(at(5), EVERY_2H), at(22, day=1))


class RequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        patcher = patch.object(worker, "STATE_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = datetime.now(timezone.utc)

    def drop(self, **request):
        body = {"schema_version": 1, "id": "abc", "requested_at": self.now.isoformat(), **request}
        (self.dir / "request.json").write_text(json.dumps(body), encoding="utf-8")

    def test_valid_request_is_consumed(self):
        self.drop(target="nvr-03")
        job, rejected = worker.take_request({"nvr-01", "nvr-03"}, self.now)
        self.assertEqual((job["target"], job["trigger"], job["email"], rejected), ("nvr-03", "manual", False, None))
        self.assertFalse((self.dir / "request.json").exists())

    def test_unknown_or_malformed_targets_are_refused(self):
        for target in ("nvr-99", "../etc", "nvr-01; rm -rf /", 7, None):
            with self.subTest(target=target):
                self.drop(target=target)
                job, rejected = worker.take_request({"nvr-01"}, self.now)
                self.assertIsNone(job)
                self.assertIsNotNone(rejected)

    def test_old_request_expires(self):
        self.drop(target="all", requested_at=(self.now - timedelta(hours=2)).isoformat())
        job, rejected = worker.take_request({"nvr-01"}, self.now)
        self.assertIsNone(job)
        self.assertIn("expired", rejected)

    def test_commands_are_fixed_argument_lists(self):
        class Nvr:
            id, ip, http_port = "nvr-03", "192.0.2.3", 80
        self.assertEqual(worker.command_for({"target": "all", "email": False}, [Nvr])[-2:],
                         ["scripts.run_all_nvr_reports", "--no-email"])
        self.assertEqual(worker.command_for({"target": "all", "email": True}, [Nvr])[-1], "scripts.run_all_nvr_reports")
        self.assertEqual(worker.command_for({"target": "nvr-03", "email": False}, [Nvr])[-4:],
                         ["--ip", "192.0.2.3", "--http-port", "80"])

    def test_output_is_reduced_to_fixed_codes(self):
        all_out = "Starting nvr-01 at 10.0.0.1:80\nTotal: 12 | Execution errors: 0\nEmail report: SENT to 1 recipient(s).\n"
        self.assertEqual(worker.summarize_output({"target": "all"}, all_out, 1),
                         {"result": "OK", "execution_errors": 0, "email_result": "SENT"})
        one_out = "Overall status: WARNING\nJSON report saved: /app/reports/x.json\n"
        self.assertEqual(worker.summarize_output({"target": "nvr-01"}, one_out, 1), {"result": "OK", "overall": "WARNING"})
        self.assertEqual(worker.summarize_output({"target": "nvr-01"}, "Another report is already running", 2), {"result": "BUSY"})
        self.assertEqual(worker.summarize_output({"target": "nvr-01"}, "", None), {"result": "TIMEOUT"})


class FleetNoEmailTests(unittest.TestCase):
    @patch.object(fleet, "send_email_report")
    @patch.object(fleet, "load_inventory", return_value=[])
    @patch.object(fleet, "save_fleet_run_status")
    def test_no_email_flag_skips_sending(self, _save, _inventory, send):
        fleet.main(["--no-email"])
        send.assert_not_called()


class DashboardScanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        patcher = patch.object(dashboard, "SCAN_STATE_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def status(self, **extra):
        body = {"schema_version": 1, "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "on_demand_enabled": True, "scan_times": ["06:00", "08:00"], "email_times": ["08:00"],
                "current": None, "last": None, **extra}
        (self.dir / "status.json").write_text(json.dumps(body), encoding="utf-8")

    def test_request_is_written_when_scanner_is_idle(self):
        self.status()
        code, data = dashboard.request_scan("nvr-04")
        self.assertEqual(code, 202)
        request = json.loads((self.dir / "request.json").read_text())
        self.assertEqual(request["target"], "nvr-04")
        self.assertEqual(data["scan"]["queued"]["target"], "nvr-04")

    def test_refusals(self):
        self.assertEqual(dashboard.request_scan("bad id!")[0], 400)
        self.assertEqual(dashboard.request_scan("all")[0], 503)          # no scanner heartbeat
        self.status(heartbeat_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
        self.assertEqual(dashboard.request_scan("all")[0], 503)          # stale heartbeat
        self.status(on_demand_enabled=False)
        self.assertEqual(dashboard.request_scan("all")[0], 403)
        self.status(current={"target": "all", "trigger": "schedule", "progress": {"done": 1, "total": 12}})
        self.assertEqual(dashboard.request_scan("nvr-01")[0], 409)       # one scan at a time
        self.status(cooldown_until=(datetime.now(timezone.utc) + timedelta(seconds=40)).isoformat())
        self.assertEqual(dashboard.request_scan("nvr-01")[0], 429)
        self.assertFalse((self.dir / "request.json").exists())

    def test_email_flag_and_result_are_separate(self):
        self.status(last={"target": "all", "trigger": "schedule", "email": True, "email_result": "SENT",
                          "result": "OK", "finished_at": datetime.now(timezone.utc).isoformat()})
        last = dashboard.read_scan_status()["last"]
        self.assertEqual((last["email"], last["email_result"]), (True, "SENT"))

    def test_status_view_drops_unknown_fields_and_values(self):
        self.status(current={"target": "<script>", "progress": {}}, secret="x",
                    last={"target": "nvr-01", "result": "rm -rf", "trigger": "manual"})
        view = dashboard.read_scan_status()
        self.assertIsNone(view["current"])
        self.assertIsNone(view["last"]["result"])
        self.assertNotIn("secret", json.dumps(view))

    def test_http_post_needs_login_and_same_origin_header(self):
        self.status()
        with patch.object(dashboard, "DASHBOARD_USERNAME", "demo"), \
                patch.object(dashboard, "DASHBOARD_PASSWORD", "test-password-long"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}/api/scan"
            auth = "Basic " + base64.b64encode(b"demo:test-password-long").decode()
            body = json.dumps({"target": "all"}).encode()

            def post(headers):
                try:
                    with urlopen(Request(base, data=body, method="POST", headers=headers), timeout=3) as r:
                        return r.status
                except HTTPError as exc:
                    return exc.code
            try:
                json_type = {"Content-Type": "application/json"}
                self.assertEqual(post({**json_type, "X-NVR-Track": "scan"}), 401)
                self.assertEqual(post({**json_type, "Authorization": auth}), 403)
                self.assertEqual(post({**json_type, "Authorization": auth, "X-NVR-Track": "scan",
                                       "Origin": "http://evil.example"}), 403)
                self.assertEqual(post({**json_type, "Authorization": auth, "X-NVR-Track": "scan"}), 202)
                self.assertTrue((self.dir / "request.json").exists())
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
