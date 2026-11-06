"""Picture problems stay on the dashboard: Telegram posts nothing about them
unless TELEGRAM_PICTURE_ALERTS=on. No network."""
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from test_telegram_bot import Fixture, channel

from app import dashboard
from scripts import telegram_bot as tb


def moved(cid):
    return {**channel(cid), "snapshot_result": {"status": "PICTURE_VIEW_CHANGED", "message": "View differs"}}


class SettingTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(tb.load_settings({})["picture_alerts"])
        for value in ("on", "true", "YES", "1"):
            self.assertTrue(tb.load_settings({"TELEGRAM_PICTURE_ALERTS": value})["picture_alerts"])
        self.assertFalse(tb.load_settings({"TELEGRAM_PICTURE_ALERTS": "off"})["picture_alerts"])


class PictureAlertsOffTests(Fixture):
    def setUp(self):
        super().setUp()
        self.bot.settings["picture_alerts"] = False                            # The default.

    def alerts(self):
        self.calls.clear()
        self.bot.check_alerts()
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def test_no_picture_alerts_but_other_alerts_still_come(self):
        self.report(channels=[channel(1), channel(2)])
        self.alerts()
        self.report(channels=[moved(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE")])
        text = "\n".join(self.alerts())
        self.assertNotIn("Picture", text)
        self.assertIn("Went offline", text)
        self.report(channels=[channel(1), channel(2)])
        self.assertNotIn("Picture OK again", "\n".join(self.alerts()))
        self.report(channels=[moved(1), channel(2)])                            # Still on the dashboard.
        data = dashboard.read_dashboard_data()
        self.assertEqual(data["nvrs"][0]["channels"][0]["condition"]["code"], "BAD_PICTURE")

    def test_an_alert_open_before_the_change_is_dropped_without_a_message(self):
        self.bot.settings["picture_alerts"] = True
        self.report(channels=[channel(1)])
        self.alerts()
        self.report(channels=[moved(1)])
        self.assertIn("Picture view changed", self.alerts()[0])
        self.bot.settings["picture_alerts"] = False                            # Deployed with the default.
        self.assertEqual(self.alerts(), [])                                     # No "Picture OK again".
        saved = json.loads((self.bot.state_dir / "alerts.json").read_text(encoding="utf-8"))["state"]
        self.assertFalse(any(key.endswith(":pic") for key in saved))
        self.report(channels=[channel(1)])
        self.assertEqual(self.alerts(), [])

    def test_daily_summary_leaves_pictures_out_but_status_shows_them(self):
        self.report(channels=[moved(1), channel(2)])
        at = datetime.now(tb.views.LOCAL_TZ).replace(hour=8, minute=35)
        with patch.object(tb, "datetime") as clock:
            clock.now.side_effect = lambda tz=None: at
            self.calls.clear()
            self.bot.check_digest()
        digest = [p["text"] for m, p in self.calls if m == "sendMessage"]
        self.assertTrue(digest and "Daily summary" in digest[0])
        self.assertNotIn("Picture problem", digest[0])
        self.assertIn("Picture problem 1", self.message(222, "/status")[0]["text"])   # When asked.
        self.assertIn("Picture problem", self.message(222, "/problems")[0]["text"])


if __name__ == "__main__":
    unittest.main()
