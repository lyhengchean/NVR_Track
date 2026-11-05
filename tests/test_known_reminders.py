"""Known-issue reminders: weekly Telegram list, age in the email, and closing
the known issue of a camera that left the NVR's list. No network."""
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from test_telegram_bot import Fixture, channel

from app import notes
from app import telegram_views as views
from scripts import telegram_bot as tb
from scripts.run_all_nvr_reports import camera_conditions, send_email_report

SMTP_ENV = {"SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p",
            "SMTP_FROM": "a@example.com", "SMTP_TO": "b@example.com",
            "SMTP_SECURITY": "starttls", "SMTP_PORT": "587"}


def last_monday(hour=9, minute=10):
    now = datetime.now(views.LOCAL_TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now - timedelta(days=now.weekday())


class SettingTests(unittest.TestCase):
    def test_weekly_time(self):
        self.assertEqual(tb.weekly_time("mon 09:00"), (0, "09", "00"))
        self.assertEqual(tb.weekly_time("Friday  17:30"), (4, "17", "30"))
        for off in ("off", "", "OFF", "no"):
            self.assertIsNone(tb.weekly_time(off))
        for bad in ("monday", "xyz 09:00", "mon 9:00", "mon 24:00"):
            self.assertIsNone(tb.weekly_time(bad))
        self.assertEqual(tb.load_settings({})["known_reminder"], (0, "09", "00"))      # On by default.
        self.assertIsNone(tb.load_settings({"TELEGRAM_KNOWN_REMINDER": "off"})["known_reminder"])

    def test_stale_days(self):
        self.assertEqual(notes.stale_days({}), 7)
        self.assertEqual(notes.stale_days({"NVR_KNOWN_REMINDER_DAYS": "14"}), 14)
        for bad in ("0", "-3", "abc", "999"):
            self.assertEqual(notes.stale_days({"NVR_KNOWN_REMINDER_DAYS": bad}), 7)

    def test_age_uses_the_local_date(self):
        item = {"created_at": "2026-11-01T18:00:00+00:00"}          # 02 Nov 01:00 in Phnom Penh.
        self.assertEqual(notes.opened_on(item), date(2026, 11, 2))
        self.assertEqual(notes.age_days(item, date(2026, 11, 9)), 7)
        self.assertEqual(notes.age_days(item, date(2026, 11, 2)), 0)
        self.assertIsNone(notes.age_days({"created_at": "yesterday"}, date(2026, 11, 9)))
        self.assertIsNone(notes.age_days({}, date(2026, 11, 9)))
        self.assertEqual(views.age_text(item, date(2026, 11, 2)), "set today")
        self.assertEqual(views.age_text(item, date(2026, 11, 3)), "open 1 day")


class ReminderTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "notes").mkdir()
        env = patch.dict("os.environ", {"NOTES_DIR": str(self.root / "notes")})
        env.start()
        self.addCleanup(env.stop)
        self.monday = last_monday()
        # ch2 broken, ch3 recording normally again, ch9 gone from the NVR's list.
        self.report(channels=[channel(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE"), channel(3)])
        self.known("2", "Cable cut <yard>", 10)
        self.known("3", "Lens cleaning", 2, until=date.today() + timedelta(days=30))
        self.known("9", "Camera removed", 20)

    def known(self, ch, reason, days_ago, until=None):
        notes.add_known_issue("nvr-01", ch, reason, until, "Heng")
        data = json.loads((self.root / "notes" / "notes.json").read_text(encoding="utf-8"))
        for item in data["known_issues"]:
            if item["channel"] == ch:
                item["created_at"] = (self.monday - timedelta(days=days_ago)).astimezone(timezone.utc).isoformat()
        (self.root / "notes" / "notes.json").write_text(json.dumps(data), encoding="utf-8")

    def remind(self, at):
        with patch.object(tb, "datetime") as clock:
            clock.now.side_effect = lambda tz=None: at if tz else at.astimezone(timezone.utc)
            clock.fromisoformat = datetime.fromisoformat
            self.calls.clear()
            self.bot.check_known_reminder()
        return [p for m, p in self.calls if m == "sendMessage"]

    def test_weekly_reminder_once_with_age_and_latest_state(self):
        sent = self.remind(self.monday)
        self.assertEqual([p["chat_id"] for p in sent], [-100900])               # Alert chats.
        text = sent[0]["text"]
        self.assertIn("Known issues still open</b> (3)", text)
        self.assertIn("1 issue where the latest scan found no problem", text)
        self.assertIn("2 issues open 7+ days: check someone is still on them.", text)
        self.assertIn("⏳ ch2 Cam 2 — Cable cut &lt;yard&gt; · open 10 days · until marked fixed · Heng", text)
        self.assertIn("Still: Stopped recording.", text)
        self.assertIn("✅ ch3 Cam 3 — Lens cleaning · open 2 days", text)
        self.assertIn("Latest scan found no problem. Repaired? /fixed nvr-01 3", text)
        self.assertIn("⏳ ch9 — Camera removed · open 20 days", text)
        self.assertIn("Not in the latest scan (camera removed?).", text)
        self.assertNotIn("<yard>", text)
        self.assertNotIn("/fixed nvr-01 2", text)                              # Still broken: no hint.
        self.assertEqual(self.remind(self.monday + timedelta(minutes=30)), [])  # Once a week.

    def test_only_on_the_day_and_within_three_hours(self):
        self.assertEqual(self.remind(self.monday + timedelta(days=1)), [])      # Tuesday.
        self.assertEqual(self.remind(self.monday - timedelta(minutes=20)), [])  # Before 09:00.
        self.assertEqual(self.remind(self.monday + timedelta(hours=3)), [])     # Too late.
        self.bot.settings["known_reminder"] = None
        self.assertEqual(self.remind(self.monday), [])                         # Off.

    def test_no_message_when_nothing_is_open(self):
        for ch in ("2", "3", "9"):
            notes.clear_known_issue("nvr-01", ch, "Heng")
        self.assertEqual(self.remind(self.monday), [])

    def test_old_scan_never_counts_as_fixed(self):
        self.report(channels=[channel(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE"), channel(3)],
                    minutes_ago=60 * 30)                                       # Out of date.
        text = self.remind(self.monday)[0]["text"]
        self.assertNotIn("✅", text)
        self.assertIn("Latest scan found no problem (scan result out of date).", text)
        self.assertNotIn("Repaired?", text)

    def test_whole_nvr_issue(self):
        notes.add_known_issue("nvr-01", None, "Site closed", None, "Heng")
        text = self.remind(self.monday)[0]["text"]
        self.assertIn("• Whole NVR — Site closed · set today", text)
        self.assertIn("Latest scan: NVR warning, 1 camera problem.", text)

    def test_list_shows_age_and_fixed_closes_a_removed_camera(self):
        listing = self.message(222, "/known")[0]["text"]
        self.assertRegex(listing, r"ch2 Cam 2 — .*open \d+ days")
        self.assertRegex(listing, r"nvr-01 · ch9 \(not in the latest scan\) — Camera removed .*open \d+ days")
        self.assertEqual(listing.count("Lens cleaning"), 1)                    # Not listed twice.
        reply = self.message(111, "/fixed nvr-01 9 removed for good")[0]["text"]
        self.assertIn("marked fixed. Alerts are active again", reply)
        self.assertNotIn("9", {i["channel"] for i in notes.active_issues(notes.load())})
        self.assertIn("has no channel 8", self.message(111, "/fixed nvr-01 8")[0]["text"])
        self.assertIn("has no channel 9", self.message(111, "/note nvr-01 9 x")[0]["text"])   # Only /fixed.

    @patch("scripts.run_all_nvr_reports.smtplib.SMTP")
    def test_email_shows_age_and_cameras_that_look_fine(self, smtp):
        report = json.loads(next((self.root / "reports").glob("nvr-01_*.json")).read_text(encoding="utf-8"))
        conditions = camera_conditions(report, notes.load())
        self.assertEqual([k["channel"] for k in conditions["known"]], ["2"])
        self.assertEqual(conditions["known"][0]["age_days"], (date.today() - self.monday.date()).days + 10)
        self.assertEqual([k["channel"] for k in conditions["known_ok"]], ["3"])
        self.assertEqual(conditions["known_issues"], 1)                        # Counting unchanged.
        row = {"id": "nvr-01", "name": "Kiln", "ip": "192.0.2.1", "overall": "WARNING", "storage": "OK",
               "online": 2, "offline": 1, **conditions}
        with patch.dict("os.environ", {**SMTP_ENV, "NVR_KNOWN_REMINDER_DAYS": "7"}):
            send_email_report([row])
        message = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        html = message.get_body(("html",)).get_content()
        text = message.get_body(("plain",)).get_content()
        self.assertIn("<th>Open</th>", html)
        self.assertIn("1 open 7+ days: check someone is still on it.", html)
        self.assertIn(f"<strong>{conditions['known'][0]['age_days']} d since", html)
        self.assertIn("(review)", html)
        self.assertIn("Known issue, but the latest scan found no problem", html)
        self.assertIn("nvr-01 - Kiln ch3 Cam 3: Known issue: Lens cleaning", html)
        self.assertIn("Known issues (alerts paused): 1 open 7+ days, review.", text)
        self.assertIn("nvr-01 - Kiln ch3 Cam 3 - Known issue: Lens cleaning", text)


if __name__ == "__main__":
    unittest.main()
