"""Days of footage kept per NVR: median and fewest camera recording now, from
verified 30-day calendars, on the dashboard, Telegram and the email. No network."""
import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from test_telegram_bot import Fixture, calendar, channel

from app import dashboard
from app import telegram_views as views
from app.camera_condition import kept_days, kept_text
from scripts.run_all_nvr_reports import camera_conditions, send_email_report

SMTP_ENV = {"SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p",
            "SMTP_FROM": "a@example.com", "SMTP_TO": "b@example.com",
            "SMTP_SECURITY": "starttls", "SMTP_PORT": "587"}


def cam(cid, days, status="RECORDING", calendar_status="RETENTION_VERIFIED"):
    row = channel(cid, status=status, days=days)
    row["retention_result"]["status"] = calendar_status
    return row


class KeptDaysTests(unittest.TestCase):
    def test_median_fewest_and_full_window(self):
        kept = kept_days([cam(1, 30), cam(2, 21), cam(3, 21), cam(4, 9), cam(5, 25)])
        self.assertEqual((kept["cameras"], kept["typical"], kept["fewest"], kept["fewest_channels"], kept["full"]),
                         (5, 21, 9, ["4"], 1))
        self.assertEqual(kept_text(kept), "21 days (fewest 9: ch4)")
        full = kept_days([cam(1, 30), cam(2, 30), cam(3, 12)])
        self.assertEqual(kept_text(full), "30+ days (fewest 12: ch3)")
        self.assertEqual(kept_text(kept_days([cam(1, 30)])), "30+ days")

    def test_only_cameras_recording_now_with_a_verified_calendar(self):
        kept = kept_days([cam(1, 20),
                          cam(2, 2, status="NO_RECENT_RECORDING_EVIDENCE"),   # Stopped: says nothing.
                          cam(3, 1, calendar_status="RETENTION_PARTIAL"),      # Unknown days.
                          cam(4, 3, calendar_status="RETENTION_STALE"),        # Old calendar.
                          cam(5, 0)])                                          # No footage day at all.
        self.assertEqual((kept["cameras"], kept["typical"], kept["fewest"]), (1, 20, 20))
        none = kept_days([cam(1, 5, status="CHECK_ERROR"), {"channel_id": "2"}, "junk"])
        self.assertEqual(none["cameras"], 0)
        self.assertEqual(kept_text(none), "unknown (no verified calendar)")
        self.assertEqual(kept_text(None), "unknown (no verified calendar)")

    def test_fewest_lists_at_most_five_channels_in_order(self):
        kept = kept_days([cam(c, 4) for c in (12, 3, 7, 1, 9, 30)] + [cam(40, 30)])
        self.assertEqual(kept["fewest_channels"], ["1", "3", "7", "9", "12"])


class ShownEverywhereTests(Fixture):
    def setUp(self):
        super().setUp()
        self.report(channels=[cam(1, 30), cam(2, 18), cam(3, 7),
                              cam(4, 1, status="NO_RECENT_RECORDING_EVIDENCE", calendar_status="RETENTION_VERIFIED")])

    def test_dashboard_and_telegram(self):
        data = dashboard.read_dashboard_data()
        kept = data["nvrs"][0]["kept_days"]
        self.assertEqual((kept["cameras"], kept["typical"], kept["fewest"], kept["fewest_channels"]), (3, 18, 7, ["3"]))
        for text in ("keptText", "Footage kept "):
            self.assertIn(text, dashboard.HTML)
        self.assertIn("Footage kept: 18 days (fewest 7: ch3)", views.storage_text(data))
        self.assertIn("Footage kept: 18 days (fewest 7: ch3)", views.nvr_detail(data, data["nvrs"][0], False))

    @patch("scripts.run_all_nvr_reports.smtplib.SMTP")
    def test_email_column(self, smtp):
        report = json.loads(next((self.root / "reports").glob("nvr-01_*.json")).read_text(encoding="utf-8"))
        row = {"id": "nvr-01", "name": "Kiln", "ip": "192.0.2.1", "overall": "WARNING", "storage": "OK",
               "online": 4, "offline": 0, **camera_conditions(report)}
        failed = {"id": "nvr-02", "ip": "192.0.2.2", "overall": "ERROR"}
        with patch.dict("os.environ", SMTP_ENV):
            send_email_report([row, failed])
        message = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        html = message.get_body(("html",)).get_content()
        text = message.get_body(("plain",)).get_content()
        self.assertIn("<th>Footage kept</th>", html)
        self.assertIn("<td>18 days (fewest 7: ch3)</td>", html)
        self.assertIn("footage_kept=18 days (fewest 7: ch3)", text)
        self.assertNotIn("footage_kept=unknown", text)                          # Failed NVR: not "unknown".


if __name__ == "__main__":
    unittest.main()
