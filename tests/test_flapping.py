"""Cameras that keep going offline and online ("unstable connection"): counted
from saved reports, shown as an NVR warning, on the dashboard, in the email and
as one Telegram alert that pauses the camera's offline/online alerts."""
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fake_nvr import FakeNVR
from test_addons import TempDirs
from test_telegram_bot import Fixture, channel

from app import dashboard, flapping
from app import telegram_views as views
from app.reporting import determine_overall_status
from scripts import run_nvr_report as runner
from scripts.run_all_nvr_reports import camera_conditions

T0 = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)


def report(at, states):
    return {"check_completed_at": at.isoformat(),
            "channel_inventory": {"channels": [{"channel_id": cid, "name": f"Cam {cid}", "reported_status": s}
                                               for cid, s in states.items()]}}


class TrackTests(unittest.TestCase):
    def scan(self, history, at, states):
        """One scan: track against the saved reports (newest first), then save."""
        current = report(at, states)
        rows = current["channel_inventory"]["channels"]
        flapping.track(rows, history, at, 24)
        history.insert(0, current)
        return {r["channel_id"]: r for r in rows}

    def test_changes_are_chained_and_unknown_states_are_skipped(self):
        history = []
        plan = ["ONLINE", "OFFLINE", "ONLINE", "OFFLINE", "ONLINE"]
        third = ["ONLINE", "UNKNOWN", "OFFLINE", "UNKNOWN", "OFFLINE"]
        for n, state in enumerate(plan):
            rows = self.scan(history, T0 + timedelta(hours=2 * n), {"1": state, "2": "ONLINE", "3": third[n]})
        self.assertEqual(len(rows["1"]["live_changes"]), 4)
        self.assertEqual(rows["2"]["live_changes"], [])
        self.assertEqual(len(rows["3"]["live_changes"]), 1)          # ONLINE -> (unknown) -> OFFLINE.
        self.assertEqual(rows["3"]["live_last"], "OFFLINE")
        found = flapping.summary(list(rows.values()), 4, 24)
        self.assertEqual([(c["channel_id"], c["changes"]) for c in found["cameras"]], [("1", 4)])
        self.assertEqual(found["cameras"][0]["last_change"], (T0 + timedelta(hours=8)).isoformat())

    def test_old_changes_leave_the_window(self):
        history = []
        for n, state in enumerate(["ONLINE", "OFFLINE", "ONLINE", "OFFLINE", "ONLINE"]):
            self.scan(history, T0 + timedelta(hours=n), {"1": state})
        rows = self.scan(history, T0 + timedelta(hours=26), {"1": "ONLINE"})   # Changes at +1..+4 h.
        self.assertEqual(len(rows["1"]["live_changes"]), 3)                     # +1 h left the 24 h window.
        self.assertEqual(flapping.summary(list(rows.values()), 4, 24)["cameras"], [])

    def test_rebuilt_from_reports_saved_before_this_field(self):
        old = [report(T0 + timedelta(hours=h), {"1": s})
               for h, s in ((6, "OFFLINE"), (4, "ONLINE"), (2, "OFFLINE"), (0, "ONLINE"))]  # Newest first.
        old.insert(0, {"check_completed_at": (T0 + timedelta(hours=7)).isoformat()})          # A failed run.
        rows = self.scan(old, T0 + timedelta(hours=8), {"1": "ONLINE"})
        self.assertEqual(len(rows["1"]["live_changes"]), 4)
        rows = self.scan([], T0, {"1": "OFFLINE"})                                             # First report.
        self.assertEqual((rows["1"]["live_changes"], rows["1"]["live_last"]), ([], "OFFLINE"))

    def test_summary_settings_and_warning(self):
        self.assertEqual(flapping.summary(None, 4, 24)["status"], "NOT_CHECKED")
        self.assertEqual(flapping.settings({}), {"changes": 4, "hours": 24})
        self.assertEqual(flapping.settings({"NVR_FLAP_CHANGES": "6", "NVR_FLAP_HOURS": "48"}),
                         {"changes": 6, "hours": 48})
        self.assertEqual(flapping.settings({"NVR_FLAP_CHANGES": "1", "NVR_FLAP_HOURS": "x"}),
                         {"changes": 4, "hours": 24})
        self.assertIsNone(flapping.warning({"cameras": []}))
        cams = [{"channel_id": str(c), "changes": 4, "last_change": T0.isoformat()} for c in range(1, 11)]
        text = flapping.warning({"cameras": cams, "changes_needed": 4, "hours": 24})
        self.assertTrue(text.startswith("10 cameras keep going offline and online (4+ changes in 24 h): ch1,"))
        self.assertIn("ch8 and 2 more", text)
        overall = determine_overall_status(
            device_info_status="DEVICE_INFO_RETRIEVED", inventory_status="CHANNELS_RETRIEVED",
            channel_status="CHANNEL_STATUS_RETRIEVED", storage_status="HEALTHY", offline_count=0,
            expected_count=1, actual_count=1, recording_status="RECORDING_EVIDENCE_FOUND",
            flapping={"cameras": cams[:1], "changes_needed": 4, "hours": 24})
        self.assertEqual(overall["status"], "WARNING")
        self.assertIn("1 camera keeps going offline and online (4+ changes in 24 h): ch1.", overall["warnings"][0])


class EndToEndTests(TempDirs):
    """The real reporter against a fake NVR whose ch2 keeps dropping."""

    def setUp(self):
        super().setUp()
        self.nvr = FakeNVR()
        self.addCleanup(self.nvr.close)
        (self.root / "config" / "nvrs.local.json").write_text(json.dumps({"schema_version": 1, "nvrs": [
            {"id": "nvr-01", "name": "Kiln", "ip": "127.0.0.1", "http_port": self.nvr.port,
             "expected_channels": 4}]}))
        for target, value in (("ROOT", self.root), ("INVENTORY_PATH", self.root / "config" / "nvrs.local.json")):
            patcher = patch.object(runner, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict("os.environ", {"NVR_USERNAME": "admin", "NVR_PASSWORD": "not-a-real-password",
                                        "NVR_PER_CAMERA_RECORDING_IDS": "nvr-01",
                                        "NVR_RECORDING_RETRY_DELAY_SECONDS": "0"})
        env.start()
        self.addCleanup(env.stop)

    def run_report(self) -> dict:
        before = set((self.root / "reports").glob("nvr-01_*.json"))
        with patch.object(sys, "argv", ["run_nvr_report", "--ip", "127.0.0.1", "--http-port", str(self.nvr.port)]), \
                patch("sys.stdout", new_callable=io.StringIO):
            runner.main()
        new = sorted(set((self.root / "reports").glob("nvr-01_*.json")) - before)
        return json.loads(new[0].read_text(encoding="utf-8"))

    def test_report_dashboard_and_email(self):
        for online in (True, False, True, False):
            self.nvr.channels["2"] = ("Yard", online)
            saved = self.run_report()
            self.assertEqual(saved["flapping"]["cameras"], [])                  # 0..3 changes.
        self.nvr.channels["2"] = ("Yard", True)
        saved = self.run_report()
        self.assertEqual([(c["channel_id"], c["changes"]) for c in saved["flapping"]["cameras"]], [("2", 4)])
        self.assertTrue(any("keeps going offline and online" in w for w in saved["overall"]["warnings"]))
        self.assertEqual({m for m, _ in self.nvr.requests} - {"GET", "POST"}, set())
        cams = {c["channel_id"]: c for c in dashboard.read_dashboard_data()["nvrs"][0]["channels"]}
        self.assertEqual((cams["2"]["unstable"]["changes"], cams["2"]["unstable"]["hours"]), (4, 24))
        self.assertIsNone(cams["1"]["unstable"])
        self.assertIn("Unstable connection: ", dashboard.HTML)
        self.assertTrue(any("keeps going offline and online" in f for f in camera_conditions(saved)["findings"]))


class TelegramTests(Fixture):
    def unstable_report(self, live="OFFLINE", changes=5):
        cam = channel(2, live=live)
        cam["live_changes"] = [(datetime.now(timezone.utc) - timedelta(hours=h)).isoformat() for h in range(changes)]
        self.report(channels=[channel(1), cam])
        path = next((self.root / "reports").glob("nvr-01_*.json"))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["flapping"] = flapping.summary(data["channel_inventory"]["channels"], 4, 24)
        path.write_text(json.dumps(data), encoding="utf-8")

    def alerts(self):
        self.calls.clear()
        self.bot.check_alerts()
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def test_one_alert_instead_of_offline_online_at_every_scan(self):
        self.report(channels=[channel(1), channel(2)])
        self.alerts()                                                           # Baseline.
        self.report(channels=[channel(1), channel(2, live="OFFLINE")])
        self.assertIn("Went offline", self.alerts()[0])
        self.unstable_report("OFFLINE")
        text = self.alerts()[0]
        self.assertIn("〰️ Unstable connection:", text)
        self.assertIn("5 online/offline changes in 24 h", text)
        self.assertNotIn("Back online", text)                                  # The offline alert is paused, not resolved.
        self.unstable_report("ONLINE")
        self.assertEqual(self.alerts(), [])                                     # Back online: no message.
        self.unstable_report("OFFLINE")
        self.assertEqual(self.alerts(), [])
        self.report(channels=[channel(1), channel(2, live="OFFLINE")])          # Stable (offline) now.
        text = self.alerts()[0]
        self.assertIn("No longer going offline and online", text)
        self.assertIn("Went offline", text)

    def test_problems_and_camera_view(self):
        self.unstable_report("ONLINE")
        problems = self.message(222, "/problems")[0]["text"]
        self.assertIn("〰️ ch2 Cam 2 — Unstable connection: 5 online/offline changes in 24 h", problems)
        self.assertIn("〰️ Unstable connection: 5 online/offline changes", self.message(222, "/camera nvr-01 2")[0]["text"])


if __name__ == "__main__":
    unittest.main()
