"""A failed fleet run must never silently count old camera data as current."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

from app import dashboard
from scripts import run_all_nvr_reports as fleet


class FleetRunReconciliationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def report(self, nvr, completed, *, online, offline, status):
        channels = ([{"channel_id": str(i), "reported_status": "ONLINE",
                      "recording_result": {"status": "RECORDING"}}
                     for i in range(1, online + 1)] +
                    [{"channel_id": str(i+online), "reported_status": "OFFLINE",
                      "recording_result": {"status": "NO_RECENT_RECORDING_EVIDENCE"}}
                     for i in range(1, offline + 1)])
        path = self.root / f"{nvr}_{completed[:10]}.json"
        path.write_text(json.dumps({
            "nvr": {"id": nvr}, "check_completed_at": completed,
            "overall": {"status": status},
            "channel_status": {"online_count": online, "offline_count": offline},
            "channel_inventory": {"channels": channels},
            "storage": {"status": "CRITICAL" if status == "CRITICAL" else "WARNING"},
        }), encoding="utf-8")

    def status_manifest(self, failed, started="2026-10-28T08:00:00+07:00"):
        rows = [{"id": "nvr-01", "overall": "CRITICAL"},
                {"id": "nvr-04", "overall": "ERROR", "failure_code": "PROCESS_TIMEOUT"}]
        if not failed:
            rows[1]["overall"] = "WARNING"
        with patch.object(fleet, "REPORTS_DIR", self.root):
            fleet.save_fleet_run_status(started, rows)

    def test_failed_nvr_displays_previous_details_but_not_in_fresh_totals(self):
        # Relative times keep "fresh" (< 26 h) true whenever the test runs.
        run_started = datetime.now(ZoneInfo("Asia/Phnom_Penh")) - timedelta(hours=1)
        self.report("nvr-01", (run_started + timedelta(seconds=20)).isoformat(),
                    online=1, offline=0, status="CRITICAL")
        self.report("nvr-04", (run_started - timedelta(hours=22)).isoformat(),
                    online=30, offline=16, status="CRITICAL")
        self.status_manifest(True, run_started.isoformat())
        with patch.object(dashboard, "REPORTS_DIR", self.root):
            data = dashboard.read_dashboard_data()
        summary = data["summary"]
        self.assertEqual(summary["total_nvrs"], 2)
        self.assertEqual(summary["latest_run_reporting"], 1)
        self.assertEqual(summary["latest_run_failed"], 1)
        self.assertEqual(summary["cameras_online"], 1)
        self.assertEqual(summary["cameras_offline"], 0)
        self.assertEqual(summary["recording_evidence"], 1)
        self.assertEqual(summary["recording_no_evidence"], 0)
        self.assertEqual(summary["critical"], 1)
        self.assertEqual(summary["stale_reports"], 1)
        self.assertEqual(summary["last_known_cameras_on_failed_nvrs"], 46)
        failed = next(nvr for nvr in data["nvrs"] if nvr["id"] == "nvr-04")
        self.assertEqual(failed["overall_status"], "ERROR")
        self.assertTrue(failed["last_run_failed"])
        self.assertEqual(failed["last_run_failure_code"], "PROCESS_TIMEOUT")
        self.assertEqual(failed["last_known_overall_status"], "CRITICAL")
        self.assertEqual(len(failed["channels"]), 46)
        self.assertIn("Latest fleet run failed", dashboard.HTML)
        self.assertNotIn("innerHTML", dashboard.HTML)

    def test_newer_manual_report_clears_old_fleet_failure(self):
        self.report("nvr-04", "2026-10-28T08:05:00+07:00", online=30, offline=16, status="WARNING")
        self.status_manifest(True)
        with patch.object(dashboard, "REPORTS_DIR", self.root):
            data = dashboard.read_dashboard_data()
        self.assertEqual(data["summary"]["latest_run_failed"], 0)
        self.assertEqual(data["summary"]["cameras_online"], 30)
        self.assertFalse(data["nvrs"][0]["last_run_failed"])

    def test_backward_compatibility_no_manifest(self):
        self.report("nvr-04", "2026-10-27T10:09:00+07:00", online=1, offline=1, status="WARNING")
        with patch.object(dashboard, "REPORTS_DIR", self.root):
            data = dashboard.read_dashboard_data()
        self.assertEqual(data["summary"]["latest_run_failed"], 0)
        self.assertEqual(data["summary"]["cameras_online"], 1)

    def test_corrupt_manifest_does_not_hide_reports(self):
        self.report("nvr-04", "2026-10-27T10:09:00+07:00", online=1, offline=1, status="WARNING")
        (self.root / "fleet_latest_run.json").write_text("{corrupt", encoding="utf-8")
        with patch.object(dashboard, "REPORTS_DIR", self.root):
            data = dashboard.read_dashboard_data()
        self.assertEqual(data["report_errors"], 1)
        self.assertEqual(data["summary"]["cameras_online"], 1)

    def test_manifest_has_no_ips_credentials_or_freeform_diagnostics(self):
        self.status_manifest(True)
        data = (self.root / "fleet_latest_run.json").read_text()
        self.assertNotIn("password", data)
        self.assertNotIn("10.233", data)
        self.assertNotIn("ip", data)
        self.assertIn("PROCESS_TIMEOUT", data)


class FleetEmailTests(unittest.TestCase):
    @patch.dict(os.environ, {"SMTP_HOST": "example.invalid", "SMTP_PORT": "587",
                             "SMTP_SECURITY": "starttls", "SMTP_USERNAME": "demo",
                             "SMTP_PASSWORD": "example-secret", "SMTP_FROM": "sender@example.invalid",
                             "SMTP_TO": "receiver@example.invalid"})
    @patch("scripts.run_all_nvr_reports.smtplib.SMTP")
    def test_mail_subject_and_body_distinguish_failed_report(self, smtp):
        rows = [{"id": "nvr-01", "overall": "CRITICAL", "online": 45,
                 "offline": 11, "recording_evidence": 47, "recording_missing": 9},
                {"id": "nvr-04", "overall": "ERROR", "online": 0,
                 "offline": 0, "recording": "NOT_REPORTED"}]
        sent, _ = fleet.send_email_report(rows)
        self.assertTrue(sent)
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertIn("1 report error", message["Subject"])
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("Fresh reports: 1 of 2 NVRs", html)
        self.assertIn("current camera conditions are unknown", html)
        self.assertIn("<td>—</td>", html)
        self.assertNotIn("example-secret", str(message))


if __name__ == "__main__":
    unittest.main()
