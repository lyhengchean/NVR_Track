"""One verdict per camera, shared by the dashboard and the daily email."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from nvr_track.web import dashboard
from nvr_track.monitoring.camera_condition import classify, count
from nvr_track.cli.run_all_nvr_reports import camera_conditions, send_email_report
from frontend_source import DASHBOARD_UI

AS_OF = date(2026, 11, 1)


def calendar(pattern, status="RETENTION_VERIFIED"):
    days = [(AS_OF - timedelta(days=len(pattern) - 1 - i)).isoformat() for i in range(len(pattern))]
    return {"status": status, "window_days": len(pattern),
            "days_with_footage": sum(v is True for v in pattern),
            "calendar": [{"date": d, "has_recording": v} for d, v in zip(days, pattern)]}


def camera(recording, live="ONLINE", pattern=(True,) * 30, history="HISTORICAL_NOT_CHECKED", **extra):
    return {"channel_id": "1", "name": "Gate", "reported_status": live,
            "recording_result": {"status": recording},
            "historical_recording_result": {"status": history},
            "retention_result": calendar(list(pattern)), **extra}


class ClassifyTests(unittest.TestCase):
    def code(self, *args, **kwargs):
        return classify(camera(*args, **kwargs))["code"]

    def test_recording_every_day_is_ok(self):
        self.assertEqual(self.code("RECORDING"), "OK")

    def test_missing_days_after_first_recording_are_gaps(self):
        result = classify(camera("RECORDING", pattern=[True] * 10 + [False] * 10 + [True] * 10))
        self.assertEqual((result["code"], result["missing_days"]), ("GAPS", 10))

    def test_footage_only_for_recent_days_is_short_history(self):
        result = classify(camera("RECORDING", pattern=[False] * 17 + [True] * 13))
        self.assertEqual((result["code"], result["history_days"]), ("SHORT_HISTORY", 13))

    def test_offline_camera_with_recent_video(self):
        self.assertEqual(self.code("RECORDING", live="OFFLINE"), "OFFLINE_RECORDING")

    def test_no_video_on_any_day_is_no_footage_30(self):
        self.assertEqual(self.code("NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE",
                                   pattern=[False] * 30), "NO_FOOTAGE_30")

    def test_older_video_means_stopped_recording(self):
        self.assertEqual(self.code("NO_RECENT_RECORDING_EVIDENCE", pattern=[False] * 25 + [True] * 5),
                         "NOT_RECORDING")
        self.assertEqual(self.code("NO_RECENT_RECORDING_EVIDENCE", pattern=[False] * 30,
                                   history="HISTORICAL_RECORDING_FOUND"), "NOT_RECORDING")

    def test_failed_or_missing_checks_are_never_ok(self):
        for status in ("CHECK_ERROR", "TRACK_MAPPING_UNVERIFIED", None):
            self.assertEqual(self.code(status), "CHECK_FAILED", status)
        # Disabled checks are unknown, not failed, and never OK.
        self.assertEqual(self.code("NOT_CONFIGURED"), "NOT_MONITORED")

    def test_unknown_calendar_does_not_invent_problems(self):
        channel = camera("RECORDING")
        channel["retention_result"] = {"status": "RETENTION_PENDING"}
        self.assertEqual(classify(channel)["code"], "OK")
        channel["recording_result"]["status"] = "NO_RECENT_RECORDING_EVIDENCE"
        self.assertEqual(classify(channel)["code"], "NOT_RECORDING")

    def test_unknown_days_are_not_counted_as_gaps(self):
        self.assertEqual(self.code("RECORDING", pattern=[True] * 15 + [None] * 5 + [True] * 10), "OK")

    def test_count_covers_every_condition(self):
        totals = count([camera("RECORDING"), camera("CHECK_ERROR")])
        self.assertEqual((totals["OK"], totals["CHECK_FAILED"], sum(totals.values())), (1, 1, 2))


class DashboardAndEmailTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.channels = [
            camera("RECORDING"),
            dict(camera("NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE", pattern=[False] * 30),
                 channel_id="2", name="Coal <tippler>"),
            dict(camera("RECORDING", pattern=[True] * 10 + [False] * 10 + [True] * 10), channel_id="3"),
        ]

    def test_dashboard_adds_conditions_and_totals(self):
        report = {"schema_version": 2, "nvr": {"id": "nvr-01", "ip": "192.0.2.1"},
                  "check_completed_at": "2026-11-02T08:00:00+07:00", "overall": {"status": "WARNING"},
                  "channel_inventory": {"channels": self.channels}, "storage": {"status": "OK", "disks": []}}
        (self.root / "nvr-01_20261102T080000+0700.json").write_text(json.dumps(report), encoding="utf-8")
        with patch.object(dashboard, "REPORTS_DIR", self.root):
            data = dashboard.read_dashboard_data()
        codes = [ch["condition"]["code"] for ch in data["nvrs"][0]["channels"]]
        self.assertEqual(codes, ["OK", "NO_FOOTAGE_30", "GAPS"])
        self.assertEqual(data["summary"]["condition_counts"]["NO_FOOTAGE_30"], 1)
        self.assertEqual(data["nvrs"][0]["condition_counts"]["GAPS"], 1)
        for text in ("condition-filter", "all-columns", "only-problems", "No footage 30+ days"):
            self.assertIn(text, DASHBOARD_UI)

    @patch("nvr_track.cli.run_all_nvr_reports.smtplib.SMTP")
    def test_email_lists_cameras_that_need_action(self, smtp):
        row = {"id": "nvr-01", "name": "CCR", "ip": "192.0.2.1", "overall": "WARNING", "storage": "OK",
               "online": 2, "offline": 1,
               **camera_conditions({"channel_inventory": {"channels": self.channels}})}
        self.assertEqual((row["no_footage_30"], row["gaps"], row["stopped_recording"]), (1, 1, 0))
        env = {"SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p",
               "SMTP_FROM": "a@example.com", "SMTP_TO": "b@example.com",
               "SMTP_SECURITY": "starttls", "SMTP_PORT": "587"}
        with patch.dict("os.environ", env):
            send_email_report([row])
        message = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        html = message.get_body(("html",)).get_content()
        text = message.get_body(("plain",)).get_content()
        self.assertIn("Cameras that need action", html)
        self.assertIn("Coal &lt;tippler&gt;", html)
        self.assertNotIn("Coal <tippler>", html)
        self.assertIn("No footage 30+ days: nvr-01 - CCR ch2 Coal <tippler>", text)


if __name__ == "__main__":
    unittest.main()
