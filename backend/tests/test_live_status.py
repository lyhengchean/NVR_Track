"""Live status between scans (nvr-live): camera online/offline, disk states and
NVR reachability within minutes, read-only, without false "back online",
"OK again" or "reachable again" when a live check fails."""
import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fake_nvr import FakeNVR
from test_telegram_bot import Fixture, channel

from nvr_track.web import dashboard
from nvr_track.monitoring import live
from nvr_track.core.config import NVRConfig
from nvr_track.services import live_status
from nvr_track.core.paths import PROJECT_ROOT as ROOT
from frontend_source import DASHBOARD_UI

NOW = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
CREDENTIALS = {"NVR_USERNAME": "admin", "NVR_PASSWORD": "not-a-real-password"}


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def config(port: int) -> NVRConfig:
    return NVRConfig(id="nvr-01", ip="127.0.0.1", expected_channels=4, http_port=port,
                     expected_sata_hdd_ids=(1, 2))


class LiveCheckTests(unittest.TestCase):
    """nvr_track.services.live_status.check against a fake NVR."""

    def setUp(self):
        self.nvr = FakeNVR()
        self.addCleanup(self.nvr.close)
        env = patch.dict("os.environ", CREDENTIALS)
        env.start()
        self.addCleanup(env.stop)

    def check(self, previous=None, port=None):
        return live_status.check(config(port or self.nvr.port), previous, 60, 24, 60)

    def test_two_read_only_gets_give_camera_and_disk_states(self):
        first = self.check()
        self.assertEqual(self.nvr.requests, [("GET", "/ISAPI/ContentMgmt/InputProxy/channels/status"),
                                             ("GET", "/ISAPI/ContentMgmt/Storage/hdd")])
        self.assertEqual((first["reachable"], first["failures"], first["channels_ok"], first["storage_ok"]),
                         (True, 0, True, True))
        self.assertEqual(first["channels"], {"1": "ONLINE", "2": "ONLINE", "3": "ONLINE", "4": "OFFLINE"})
        self.assertEqual(first["storage"]["status"], "HEALTHY")
        self.assertEqual([d["reported_status"] for d in first["storage"]["disks"]], ["ok", "ok"])
        self.assertIsNone(live_status.describe(first, None))

        self.nvr.channels["2"] = ("Yard", False)
        self.nvr.channels["4"] = ("Roof", True)
        self.nvr.disk_states["2"] = "error"
        second = self.check(first)
        self.assertEqual((second["channels"]["2"], second["channels"]["4"]), ("OFFLINE", "ONLINE"))
        self.assertEqual(second["storage"]["status"], "CRITICAL")
        self.assertIn("2 (error)", second["storage"]["message"])
        self.assertEqual(set(second["changes"]), {"2", "4"})
        self.assertEqual(live_status.describe(second, first), "live: nvr-01 offline ch2 back online ch4")
        self.assertEqual({method for method, _ in self.nvr.requests}, {"GET"})

    def test_unreachable_after_two_failed_checks_keeps_last_good_states(self):
        good = self.check()
        port = closed_port()
        one = self.check(good, port)
        self.assertEqual((one["reachable"], one["failures"], one["error"]), (False, 1, "CONNECTION_ERROR"))
        self.assertEqual((one["channels_ok"], one["storage_ok"]), (False, False))
        self.assertEqual(one["channels"], good["channels"])                   # Last good data stays.
        self.assertEqual(one["channels_at"], good["channels_at"])
        self.assertIsNone(live_status.describe(one, good))
        two = self.check(one, port)
        self.assertEqual((two["failures"], two["down_since"]), (2, one["down_since"]))
        self.assertEqual(live_status.describe(two, one), "live: nvr-01 not reachable (CONNECTION_ERROR)")
        back = self.check(two)
        self.assertEqual((back["reachable"], back["failures"], back["down_since"]), (True, 0, None))
        self.assertEqual(live_status.describe(back, two), "live: nvr-01 reachable again")

    def test_an_answer_without_usable_data_is_unknown_not_down(self):
        refused = SimpleNamespace(status="AUTHENTICATION_FAILED", http_status=401, channels=())
        storage = SimpleNamespace(status="UNKNOWN", http_status=401, disks=(), message="Authentication failed.")
        entry = live.next_entry(None, "nvr-01", 60, refused, storage, now=NOW)
        self.assertEqual((entry["reachable"], entry["failures"], entry["error"]), (None, 0, "AUTHENTICATION_FAILED"))
        # Channels unreadable but disks read: reachable, channel data unknown.
        timeout = SimpleNamespace(status="TIMEOUT", http_status=None, channels=())
        disks = SimpleNamespace(status="HEALTHY", http_status=200, message="ok",
                                disks=(SimpleNamespace(id="1", name="hdd1", disk_type="SATA", status="ok",
                                                       capacity_mb=1, free_space_mb=1, property="RW", group="1"),))
        entry = live.next_entry({**entry, "channels": {"1": "ONLINE"}, "channels_at": NOW.isoformat()},
                                "nvr-01", 60, timeout, disks, now=NOW + timedelta(minutes=1))
        self.assertEqual((entry["reachable"], entry["channels_ok"], entry["storage_ok"]), (True, False, True))
        self.assertEqual(entry["channels"], {"1": "ONLINE"})

    def test_changes_leave_the_window_and_unknown_is_no_change(self):
        old = {"channels": {"1": "ONLINE", "2": "OFFLINE"},
               "changes": {"1": [(NOW - timedelta(hours=25)).isoformat(), (NOW - timedelta(hours=1)).isoformat()]}}
        changes = live.record_changes(old, {"1": "OFFLINE", "2": "UNKNOWN"}, NOW, 24)
        self.assertEqual(changes, {"1": [(NOW - timedelta(hours=1)).isoformat(), NOW.isoformat()]})


class FilesAndSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def test_settings(self):
        self.assertEqual(live.interval_seconds({}), 60)
        self.assertEqual(live.interval_seconds({"NVR_LIVE_SECONDS": "120"}), 120)
        self.assertEqual(live.interval_seconds({"NVR_LIVE_SECONDS": "5"}), 60)
        self.assertEqual(live.interval_seconds({"NVR_LIVE_SECONDS": "x"}), 60)
        self.assertEqual(live.selected_ids({"NVR_LIVE_NVR_IDS": " NVR-04, nvr-01 ,"}), {"nvr-04", "nvr-01"})

    def test_entries_service_heartbeat_and_cleanup(self):
        live.write_entry({"schema_version": 1, "nvr": "nvr-01", "checked_at": NOW.isoformat()}, self.dir)
        live.write_entry({"schema_version": 1, "nvr": "nvr-02", "checked_at": NOW.isoformat()}, self.dir)
        (self.dir / "nvr-03.json").write_text('{"schema_version": 1, "nvr": "nvr-09"}')   # Wrong name: ignored.
        (self.dir / "nvr-04.json").write_text("not json")
        live.write_service(["nvr-01"], 60, self.dir)
        service, entries = live.read_all(self.dir)
        self.assertEqual(sorted(entries), ["nvr-01", "nvr-02"])
        self.assertEqual(service["nvrs"], ["nvr-01"])
        self.assertTrue(live.service_alive(service))
        self.assertFalse(live.service_alive(service, datetime.now(timezone.utc) + timedelta(minutes=6)))
        self.assertFalse(live.service_alive({}))
        with patch.dict("os.environ", {"LIVE_DIR": str(self.dir)}):
            live_status.remove_unlisted({"nvr-01"})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["nvr-01.json", "service.json"])

    def test_compose_and_deploy(self):
        if not (ROOT / "compose.yaml").exists():
            self.skipTest("compose.yaml is not part of this test image")
        text = (ROOT / "compose.yaml").read_text(encoding="utf-8")

        def block(name):
            return text.split(f"  {name}:", 1)[1].split("\n  nvr-", 1)[0]
        svc = block("nvr-live")
        self.assertIn('entrypoint: ["python", "-m", "nvr_track.services.live_status"]', svc)
        self.assertIn("- ./state/live:/app/state/live\n", svc)
        self.assertIn("- ./config/nvrs.local.json:/app/config/nvrs.local.json:ro", svc)
        self.assertIn('"/app/state/live/service.json"', svc)
        for name in ("nvr-dashboard", "nvr-telegram"):
            self.assertIn("- ./state/live:/app/state/live:ro", block(name))
            self.assertIn("LIVE_DIR: /app/state/live", block(name))
        self.assertNotIn("env_file", block("nvr-dashboard"))                   # Still no NVR credentials.
        self.assertNotIn("env_file", block("nvr-telegram"))
        deploy = (ROOT / "scripts" / "deploy_check.sh").read_text()
        self.assertIn("NVR_LIVE_NVR_IDS", deploy)
        self.assertIn("nvr-live", deploy)


def report(minutes_ago=10, states=None, storage="HEALTHY", evidence=None):
    states = states or {"1": "ONLINE", "2": "ONLINE", "3": "ONLINE"}
    return {
        "check_completed_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "nvr": {"id": "nvr-01"},
        "channel_inventory": {"channels": [{"channel_id": c, "name": f"Cam {c}", "reported_status": s}
                                           for c, s in states.items()]},
        "channel_status": {"status": "CHANNEL_STATUS_RETRIEVED", "online_count": 3, "offline_count": 0},
        "storage": {"status": storage, "message": "", "disks": [], "smart": {"status": "HEALTHY"}},
        "per_camera_recording": {"enabled": evidence is not None, "evidence": evidence or 0},
        "flapping": {"status": "CHECKED", "changes_needed": 4, "hours": 24, "cameras": []},
        "overall": {"status": "WARNING", "critical_reasons": [], "unknown_checks": [],
                    "warnings": ["2 channels are reported offline.", "Camera list changed since the previous report: 1 added."]},
    }


def entry(minutes_ago=0, channels=None, storage=None, **extra):
    at = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {"schema_version": 1, "nvr": "nvr-01", "interval_seconds": 60, "checked_at": at,
            "reachable": True, "error": None, "failures": 0, "down_since": None,
            "channels_ok": True, "storage_ok": storage is not None, "channels_at": at,
            "channels": channels or {"1": "ONLINE", "2": "OFFLINE", "3": "ONLINE"},
            "storage_at": at if storage else None, "storage": storage, "changes": {}, **extra}


def disks(*states):
    return [{"id": str(i), "name": f"hdd{i}", "type": "SATA", "reported_status": s, "capacity_mb": 4,
             "free_space_mb": 1, "property": "RW", "group": "1"} for i, s in enumerate(states, 1)]


class OverlayTests(unittest.TestCase):
    def test_newer_live_states_replace_the_report_and_its_findings(self):
        storage = {"status": "CRITICAL", "message": "NVR reports abnormal HDD states: 2 (error).",
                   "status_writing": "CRITICAL", "message_writing": "NVR reports abnormal HDD states: 2 (error).",
                   "disks": disks("ok", "error")}
        shown = live.overlay(report(), entry(channels={"1": "ONLINE", "2": "OFFLINE", "3": "UNKNOWN"},
                                             storage=storage), now=NOW)
        rows = {r["channel_id"]: r["reported_status"] for r in shown["channel_inventory"]["channels"]}
        self.assertEqual(rows, {"1": "ONLINE", "2": "OFFLINE", "3": "UNKNOWN"})
        self.assertEqual((shown["channel_status"]["online_count"], shown["channel_status"]["offline_count"],
                          shown["channel_status"]["unknown_count"]), (1, 1, 1))
        self.assertEqual(shown["storage"]["status"], "CRITICAL")
        self.assertEqual(shown["storage"]["smart"], {"status": "HEALTHY"})     # SMART stays from the scan.
        overall = shown["overall"]
        self.assertEqual(overall["status"], "CRITICAL")
        self.assertEqual(overall["critical_reasons"],
                         ["Storage needs urgent review: NVR reports abnormal HDD states: 2 (error)."])
        self.assertEqual(overall["warnings"], ["1 channels are reported offline.",
                                               "Camera list changed since the previous report: 1 added."])
        self.assertTrue(shown["live"]["fresh"] and shown["live"]["newer"] and shown["live"]["channels_used"])

    def test_report_newer_than_live_wins_and_missing_channels_are_not_returned(self):
        older = live.overlay(report(minutes_ago=1), entry(minutes_ago=3), now=NOW)
        self.assertEqual(older["channel_inventory"]["channels"][1]["reported_status"], "ONLINE")
        self.assertFalse(older["live"]["newer"] or older["live"]["channels_used"])
        self.assertEqual(older["overall"]["status"], "WARNING")
        missing = live.overlay(report(), entry(channels={"1": "ONLINE"}), now=NOW)
        self.assertEqual([r["reported_status"] for r in missing["channel_inventory"]["channels"]],
                         ["ONLINE", "NOT_RETURNED", "NOT_RETURNED"])

    def test_full_disk_is_overwritten_when_the_scan_saw_footage(self):
        storage = {"status": "WARNING", "message": "HDD 1 is full.", "status_writing": "HEALTHY",
                   "message_writing": "Full, overwriting.", "disks": disks("ok")}
        self.assertEqual(live.overlay(report(), entry(storage=storage), now=NOW)["storage"]["status"], "WARNING")
        shown = live.overlay(report(evidence=3), entry(storage=storage), now=NOW)
        self.assertEqual((shown["storage"]["status"], shown["storage"]["message"]), ("HEALTHY", "Full, overwriting."))

    def test_live_changes_count_towards_unstable_connection(self):
        times = [(NOW - timedelta(minutes=m)).isoformat() for m in (40, 30, 20, 10)]
        shown = live.overlay(report(), entry(changes={"2": times, "3": times[:2]}), now=NOW)
        cams = shown["flapping"]["cameras"]
        self.assertEqual([(c["channel_id"], c["changes"], c["reported_status"]) for c in cams], [("2", 4, "OFFLINE")])
        self.assertTrue(any("keeps going offline and online" in w for w in shown["overall"]["warnings"]))

    def test_stale_live_data_is_flagged(self):
        shown = live.overlay(report(minutes_ago=30), entry(minutes_ago=20), now=NOW)
        self.assertTrue(shown["live"]["newer"])
        self.assertFalse(shown["live"]["fresh"])


class LiveAlertTests(Fixture):
    """Real dashboard data + the bot's alert check, with live entries from a fake NVR."""

    def setUp(self):
        super().setUp()
        (self.root / "live").mkdir()
        env = patch.dict("os.environ", {"LIVE_DIR": str(self.root / "live"), **CREDENTIALS})
        env.start()
        self.addCleanup(env.stop)
        self.nvr = FakeNVR()
        self.addCleanup(self.nvr.close)
        self.nvr.channels = {"1": ("Gate", True), "2": ("Yard", True)}
        live.write_service(["nvr-01"], 60)
        self.entry = None
        self.report(channels=[channel(1), channel(2)])
        self.assertIn("bot started", self.alerts())

    def alerts(self):
        self.calls.clear()
        self.bot.check_alerts()
        return "\n".join(p["text"] for m, p in self.calls if m == "sendMessage")

    def poll(self, port=None):
        self.entry = live_status.check(config(port or self.nvr.port), self.entry, 60, 24, 60)
        live.write_entry(self.entry)

    def test_offline_back_online_and_not_reachable_within_one_check(self):
        self.poll()
        self.assertEqual(self.alerts(), "")                                    # Nothing changed.
        self.nvr.channels["2"] = ("Yard", False)
        self.poll()
        text = self.alerts()
        self.assertIn("Went offline", text)
        self.assertIn("ch2 Cam 2", text)
        self.assertEqual(dashboard.read_dashboard_data()["nvrs"][0]["offline_channels"], 1)
        self.nvr.channels["2"] = ("Yard", True)
        self.poll()
        self.assertIn("Back online", self.alerts())
        self.nvr.channels["2"] = ("Yard", False)
        self.poll()
        self.assertIn("Went offline", self.alerts())

        # NVR unreachable: one failure is quiet, the second is "not reachable";
        # cameras are not "back online" while it cannot be read.
        port = closed_port()
        self.poll(port)
        self.assertEqual(self.alerts(), "")
        self.poll(port)
        text = self.alerts()
        self.assertIn("is not reachable since", text)
        self.assertIn("connection refused or network down", text)
        self.assertNotIn("Back online", text)
        self.assertNotIn("is critical", text)                                  # Said once, not twice.
        shown = dashboard.read_dashboard_data()["nvrs"][0]
        self.assertEqual(shown["overall_status"], "CRITICAL")                  # Not the last scan's "Warning".
        self.assertTrue(shown["critical_reasons"][0].startswith("Not reachable by the live check since "))
        self.assertEqual(dashboard.read_dashboard_data()["summary"]["live_unreachable"], 1)

        self.nvr.channels["2"] = ("Yard", True)
        self.poll()
        text = self.alerts()
        self.assertIn("is reachable again", text)
        self.assertIn("Back online", text)
        self.assertNotIn("critical", text)
        self.assertNotEqual(dashboard.read_dashboard_data()["nvrs"][0]["overall_status"], "CRITICAL")

    def test_each_disk_alerts_when_it_fails_and_when_it_is_ok_again(self):
        self.nvr.disk_states = {"1": "ok", "2": "error", "3": "ok"}
        self.poll()
        text = self.alerts()
        self.assertIn("HDD 2: error", text)
        self.assertNotIn("storage critical", text)       # The disk alert says it once.
        self.assertNotIn("is critical", text)
        self.nvr.disk_states["3"] = "smartfailed"
        self.poll()
        text = self.alerts()
        self.assertIn("HDD 3: smartfailed", text)
        self.assertNotIn("HDD 2", text)
        self.nvr.disk_states["2"] = "ok"
        self.poll()
        text = self.alerts()
        self.assertIn("HDD 2 OK again", text)
        self.assertNotIn("HDD 3", text)
        self.nvr.disk_states["3"] = "notexist"                                 # Pulled out: not "OK again".
        self.poll()
        self.assertNotIn("OK again", self.alerts())
        self.nvr.disk_states["3"] = "ok"                                       # New disk installed.
        self.poll()
        self.assertIn("HDD 3 OK again", self.alerts())

    def test_failed_disk_read_never_says_ok_again(self):
        self.nvr.disk_states = {"1": "ok", "2": "error"}
        self.poll()
        self.assertIn("HDD 2: error", self.alerts())
        with patch.object(live_status, "get_storage_check",
                          return_value=SimpleNamespace(status="UNKNOWN", http_status=500, disks=(),
                                                       message="HTTP request returned 500.")):
            self.nvr.disk_states["2"] = "ok"
            self.poll()
        self.assertEqual(self.alerts(), "")
        self.poll()
        self.assertIn("HDD 2 OK again", self.alerts())

    def test_stopped_service_is_reported_and_holds_camera_alerts(self):
        self.nvr.channels["2"] = ("Yard", False)
        self.poll()
        self.assertIn("Went offline", self.alerts())
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        stale = {**self.entry, "checked_at": old}
        live.write_entry(stale)
        (self.root / "live" / "service.json").write_text(json.dumps(
            {"schema_version": 1, "updated_at": old, "nvrs": ["nvr-01"], "interval_seconds": 60}))
        text = self.alerts()
        self.assertIn("Live checks stopped", text)
        self.assertNotIn("Back online", text)
        live.write_service(["nvr-01"], 60)
        self.nvr.channels["2"] = ("Yard", True)
        self.poll()
        text = self.alerts()
        self.assertIn("Live checks running again", text)
        self.assertIn("Back online", text)

    def test_dashboard_exposes_live_state_and_the_page_shows_it(self):
        self.poll()
        data = dashboard.read_dashboard_data()
        self.assertEqual(data["live"]["nvrs"], ["nvr-01"])
        self.assertTrue(data["live"]["alive"])
        state = data["nvrs"][0]["live"]
        self.assertEqual((state["reachable"], state["fresh"], state["channels_used"]), (True, True, True))
        self.assertIn("Not reachable since ", DASHBOARD_UI)
        self.assertIn("function liveBadge", DASHBOARD_UI)
        self.bot.cached_data = None
        self.assertIn("Live check ", self.message(222, "/nvr 1")[0]["text"])

    def test_not_enabled_changes_nothing(self):
        (self.root / "live" / "service.json").unlink()
        self.nvr.channels["2"] = ("Yard", False)
        self.poll()                                                            # Entry exists, service off.
        self.assertEqual(self.alerts(), "")
        self.assertIsNone(dashboard.read_dashboard_data()["nvrs"][0]["live"])


if __name__ == "__main__":
    unittest.main()
