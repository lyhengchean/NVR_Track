"""Fewer useless Telegram alerts (11 Nov 2026 log): no false "no longer going
offline and online" right after a scan, short drops not posted, one line when
many NVRs drop at once, no "stopped recording" for an offline camera, and
one-scan blips (failed camera checks, a scan that could not reach the NVR)
posted only if the next scan shows them too."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from test_live_status import entry, report
from test_telegram_bot import Fixture, channel

from nvr_track.bot import telegram_bot as tb
from nvr_track.monitoring import live

NOW = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


class OverlayTests(unittest.TestCase):
    def test_live_flaps_still_count_right_after_a_scan(self):
        times = [(NOW - timedelta(minutes=m)).isoformat() for m in (40, 30, 20, 10)]
        shown = live.overlay(report(minutes_ago=1), entry(minutes_ago=3, changes={"2": times}), now=NOW)
        self.assertFalse(shown["live"]["newer"])                               # The scan is newer...
        self.assertEqual([c["channel_id"] for c in shown["flapping"]["cameras"]], ["2"])    # ...flaps still count.
        self.assertTrue(any("keeps going offline and online" in w for w in shown["overall"]["warnings"]))

    def test_flaps_older_than_the_window_do_not_count(self):
        old = [(NOW - timedelta(hours=h)).isoformat() for h in (30, 28, 26, 25)]
        shown = live.overlay(report(), entry(changes={"2": old + [(NOW - timedelta(minutes=5)).isoformat()]}), now=NOW)
        self.assertEqual(shown["flapping"]["cameras"], [])

    def test_status_since(self):
        went = (NOW - timedelta(minutes=2)).isoformat()
        shown = live.overlay(report(), entry(changes={"2": [(NOW - timedelta(hours=1)).isoformat(), went]}), now=NOW)
        rows = {r["channel_id"]: r for r in shown["channel_inventory"]["channels"]}
        self.assertEqual(rows["2"]["status_since"], went)                      # Offline since the last change.
        self.assertNotIn("status_since", rows["1"])                            # No change seen: long-standing.
        # The newer scan saw ch3 offline before any live check did: since the scan.
        scan = report(minutes_ago=1, states={"1": "ONLINE", "2": "OFFLINE", "3": "OFFLINE"})
        rows = {r["channel_id"]: r for r in live.overlay(scan, entry(minutes_ago=3), now=NOW)
                ["channel_inventory"]["channels"]}
        self.assertEqual(rows["3"]["status_since"], (NOW - timedelta(minutes=1)).isoformat())

    def test_settings(self):
        self.assertEqual(tb.load_settings({})["offline_minutes"], 5)
        self.assertEqual(tb.load_settings({"TELEGRAM_OFFLINE_MINUTES": "0"})["offline_minutes"], 0)
        self.assertEqual(tb.load_settings({"TELEGRAM_OFFLINE_MINUTES": "x"})["offline_minutes"], 5)


class AlertNoiseTests(Fixture):
    """The bot with its default settings, real dashboard data and live entries."""

    def setUp(self):
        super().setUp()
        (self.root / "live").mkdir()
        env = patch.dict("os.environ", {"LIVE_DIR": str(self.root / "live")})
        env.start()
        self.addCleanup(env.stop)
        self.assertEqual(self.settings["offline_minutes"], 5)
        live.write_service(["nvr-01", "nvr-02", "nvr-03", "nvr-04"], 60)

    def alerts(self):
        self.calls.clear()
        self.bot.check_alerts()
        return "\n".join(p["text"] for m, p in self.calls if m == "sendMessage")

    def scan(self, nvr_id="nvr-01", channels=(), minutes_ago=5, critical=(), overall="WARNING", **extra):
        self.report(nvr_id=nvr_id, channels=list(channels), minutes_ago=minutes_ago, critical=critical,
                    overall=overall)
        path = next((self.root / "reports").glob(f"{nvr_id}_*.json"))
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update({"flapping": {"status": "CHECKED", "changes_needed": 4, "hours": 24, "cameras": []}, **extra})
        path.write_text(json.dumps(data), encoding="utf-8")

    def live(self, nvr_id="nvr-01", channels=None, changes=None, down_minutes=None):
        at = datetime.now(timezone.utc).isoformat()
        down = down_minutes is not None
        live.write_entry({
            "schema_version": 1, "nvr": nvr_id, "interval_seconds": 60, "checked_at": at,
            "reachable": False if down else True, "error": "TIMEOUT" if down else None,
            "failures": 3 if down else 0, "down_since": ago(down_minutes) if down else None,
            "channels_ok": not down, "storage_ok": False, "channels_at": None if down else at,
            "channels": channels or {"1": "ONLINE", "2": "ONLINE"}, "storage_at": None, "storage": None,
            "changes": changes or {}})

    def test_unstable_camera_is_not_resolved_and_raised_again_after_each_scan(self):
        self.scan(channels=[channel(1), channel(2)])
        self.assertIn("bot started", self.alerts())
        flaps = [ago(m) for m in (300, 200, 100, 30)]
        self.live(channels={"1": "ONLINE", "2": "OFFLINE"}, changes={"2": flaps})
        self.assertIn("Unstable connection", self.alerts())
        # A scan finishes: the report is now newer than the last live check.
        self.scan(channels=[channel(1), channel(2, live="OFFLINE")], minutes_ago=0)
        self.assertEqual(self.alerts(), "")                  # No "No longer going…", no "Went offline".
        self.live(channels={"1": "ONLINE", "2": "OFFLINE"}, changes={"2": flaps})
        self.assertEqual(self.alerts(), "")                  # And no second "Unstable connection".

    def test_a_short_camera_drop_is_not_posted(self):
        self.scan(channels=[channel(1), channel(2)])
        self.alerts()
        self.live(channels={"1": "ONLINE", "2": "OFFLINE"}, changes={"2": [ago(2)]})
        self.assertEqual(self.alerts(), "")                                    # 2 minutes: not yet.
        self.live(changes={"2": [ago(3), ago(1)]})
        self.assertEqual(self.alerts(), "")                                    # Back: never said.
        self.live(channels={"1": "ONLINE", "2": "OFFLINE"}, changes={"2": [ago(6)]})
        self.assertIn("Went offline", self.alerts())                           # 6 minutes: said.
        self.live(changes={"2": [ago(6), ago(1)]})
        self.assertEqual(self.alerts(), "")                                    # Back for 1 minute: wait.
        self.live(changes={"2": [ago(12), ago(6)]})
        self.assertIn("Back online", self.alerts())

    def test_a_short_nvr_outage_is_not_posted(self):
        self.scan(channels=[channel(1), channel(2)])
        self.alerts()
        self.live(down_minutes=2)
        self.assertEqual(self.alerts(), "")
        self.live()
        self.assertEqual(self.alerts(), "")                                    # No "reachable again" either.
        self.live(down_minutes=6)
        text = self.alerts()
        self.assertIn("is not reachable since", text)
        self.assertNotIn("is critical", text)
        self.live()
        self.assertIn("is reachable again", self.alerts())

    def test_many_nvrs_down_at_once_is_one_line(self):
        nvrs = ["nvr-01", "nvr-02", "nvr-03", "nvr-04"]
        for nid in nvrs:
            self.scan(nvr_id=nid, channels=[channel(1), channel(2)])
            self.live(nid)
        self.alerts()
        for nid in nvrs:
            self.live(nid, down_minutes=6)
        text = self.alerts()
        self.assertIn("All 4 NVRs are not reachable</b> since", text)
        self.assertIn("All at the same time: check the server's own network first.", text)
        self.assertNotIn("is not reachable since", text)                       # Not one line per NVR.
        self.assertNotIn("is critical", text)
        # Live checks stop: nothing is known, so no false "reachable again".
        (self.root / "live" / "service.json").write_text(json.dumps(
            {"schema_version": 1, "updated_at": ago(10), "nvrs": nvrs, "interval_seconds": 60}))
        text = self.alerts()
        self.assertIn("Live checks stopped", text)
        self.assertNotIn("reachable again", text)
        live.write_service(nvrs, 60)
        self.assertIn("Live checks running again", self.alerts())
        for nid in nvrs[:3]:
            self.live(nid)
        text = self.alerts()
        self.assertIn("The NVRs are reachable again", text)
        self.assertIn("(nvr-04)</b> is not reachable since", text)              # Still down on its own.
        self.live("nvr-04")
        self.assertIn("(nvr-04)</b> is reachable again", self.alerts())

    def test_failed_camera_checks_are_posted_only_if_the_next_scan_has_them(self):
        self.scan(channels=[channel(1), channel(2)])
        self.alerts()
        self.scan(channels=[channel(1), channel(2, "CHECK_ERROR")], minutes_ago=4)
        self.assertEqual(self.alerts(), "")                                    # One scan: wait.
        self.scan(channels=[channel(1), channel(2)], minutes_ago=3)
        self.assertEqual(self.alerts(), "")                                    # Gone: never said.
        self.scan(channels=[channel(1), channel(2, "CHECK_ERROR")], minutes_ago=2)
        self.assertEqual(self.alerts(), "")
        self.assertEqual(self.alerts(), "")                                    # Same scan again: still waiting.
        self.scan(channels=[channel(1), channel(2, "CHECK_ERROR")], minutes_ago=1)
        self.assertIn("1 camera check could not be completed", self.alerts())  # Two scans in a row.
        self.scan(channels=[channel(1), channel(2)], minutes_ago=0)
        self.assertIn("all camera checks completed again", self.alerts())

    def test_a_scan_that_could_not_reach_the_nvr(self):
        timeout = ["Device-information check failed: TIMEOUT."]
        self.scan(channels=[channel(1), channel(2)])
        self.alerts()
        self.scan(channels=[channel(1), channel(2)], minutes_ago=4, critical=timeout, overall="CRITICAL")
        self.assertEqual(self.alerts(), "")                                    # A blip during one scan.
        self.scan(channels=[channel(1), channel(2)], minutes_ago=3, critical=timeout, overall="CRITICAL")
        self.assertIn("is critical: Device-information check failed: TIMEOUT", self.alerts())
        # Any other critical reason is posted at once.
        self.scan(nvr_id="nvr-02", channels=[channel(1)], critical=["Storage needs urgent review: x"],
                  overall="CRITICAL")
        self.assertIn("is critical: Storage needs urgent review", self.alerts())
        # While live checks find the NVR down, the reachability alert says it instead.
        self.scan(nvr_id="nvr-03", channels=[channel(1)], minutes_ago=10, critical=timeout, overall="CRITICAL")
        self.live("nvr-03", down_minutes=8)
        text = self.alerts()
        self.assertIn("(nvr-03)</b> is not reachable since", text)
        self.assertNotIn("Device-information", text)


if __name__ == "__main__":
    unittest.main()
