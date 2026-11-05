"""Add-ons: known issues, repair log, camera changes, restarts, SMART, snapshots,
real-time events and uptime. End-to-end against a fake NVR on 127.0.0.1."""
import base64
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fake_nvr import FakeNVR, event_xml, jpeg
from test_telegram_bot import TOKEN, Fixture, channel

from app import dashboard, events, notes, smart, snapshots, uptime
from app.camera_condition import classify, known_condition
from app.inventory_changes import compare_inventory, inventory_changes, restart_info
from scripts import event_listener, run_nvr_report as runner, scan_worker, uptime_report
from scripts import telegram_bot as tb
from scripts.run_all_nvr_reports import camera_conditions

ICT = timezone(timedelta(hours=7))


class TempDirs(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("reports", "notes", "events", "snapshots", "scan", "config", "tg"):
            (self.root / name).mkdir()
        env = patch.dict("os.environ", {"NOTES_DIR": str(self.root / "notes"),
                                        "EVENTS_DIR": str(self.root / "events"),
                                        "SNAPSHOT_DIR": str(self.root / "snapshots")})
        env.start()
        self.addCleanup(env.stop)
        for target, value in (("REPORTS_DIR", self.root / "reports"), ("SCAN_STATE_DIR", self.root / "scan"),
                              ("SNAPSHOT_DIR", self.root / "snapshots")):
            patcher = patch.object(dashboard, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class EndToEndTests(TempDirs):
    """The real reporter against a fake NVR, then the dashboard and the bot."""

    def setUp(self):
        super().setUp()
        self.nvr = FakeNVR()
        self.addCleanup(self.nvr.close)
        (self.root / "config" / "nvrs.local.json").write_text(json.dumps({"schema_version": 1, "nvrs": [
            {"id": "nvr-01", "name": "Kiln", "ip": "127.0.0.1", "http_port": self.nvr.port,
             "expected_channels": 4, "expected_sata_hdd_ids": [1, 2]}]}))
        for target, value in (("ROOT", self.root), ("INVENTORY_PATH", self.root / "config" / "nvrs.local.json")):
            patcher = patch.object(runner, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict("os.environ", {
            "NVR_USERNAME": "admin", "NVR_PASSWORD": "not-a-real-password",
            "NVR_PER_CAMERA_RECORDING_IDS": "nvr-01", "NVR_RETENTION_ENABLED": "true",
            "NVR_RETENTION_NVR_IDS": "nvr-01", "NVR_RETENTION_MAX_CAMERAS_PER_RUN": "8",
            "NVR_SMART_NVR_IDS": "nvr-01", "NVR_SNAPSHOT_NVR_IDS": "nvr-01",
            "NVR_RECORDING_RETRY_DELAY_SECONDS": "0", "NVR_DEFAULT_RECORDING_EXPECTATION": "continuous"})
        env.start()
        self.addCleanup(env.stop)

    def run_report(self) -> dict:
        before = set((self.root / "reports").glob("nvr-01_*.json"))
        with patch.object(sys, "argv", ["run_nvr_report", "--ip", "127.0.0.1", "--http-port", str(self.nvr.port)]), \
                patch("sys.stdout", new_callable=io.StringIO):
            runner.main()
        new = sorted(set((self.root / "reports").glob("nvr-01_*.json")) - before)
        self.assertEqual(len(new), 1)
        return json.loads(new[0].read_text(encoding="utf-8"))

    def test_full_pipeline(self):
        first = self.run_report()
        by_ch = {c["channel_id"]: c for c in first["channel_inventory"]["channels"]}
        self.assertEqual(by_ch["1"]["recording_result"]["status"], "RECORDING")
        self.assertEqual(by_ch["4"]["reported_status"], "OFFLINE")
        self.assertEqual(by_ch["1"]["snapshot_result"]["status"], "PICTURE_OK")
        self.assertEqual(by_ch["3"]["snapshot_result"]["status"], "PICTURE_BLACK")
        self.assertEqual(by_ch["4"]["snapshot_result"]["status"], "PICTURE_NOT_CHECKED")
        self.assertEqual(first["storage"]["smart"]["status"], "WARNING")      # Disk 2: 8 reallocated sectors.
        self.assertEqual(first["inventory_changes"]["status"], "FIRST_INVENTORY")
        self.assertEqual(first["device_status"]["uptime_seconds"], 10 * 86400)
        self.assertIsNone(first["restart"]["detected"])
        self.assertTrue(any("Disk SMART warning" in w for w in first["overall"]["warnings"]))
        self.assertTrue(any("picture problem" in w for w in first["overall"]["warnings"]))
        self.assertTrue((self.root / "snapshots" / "nvr-01" / "1.jpg").exists())
        # Read-only towards the NVR: only searches/calendars are POSTs.
        posts = {p for m, p in self.nvr.requests if m == "POST"}
        self.assertTrue(all(p == "/ISAPI/ContentMgmt/search" or p.endswith("/dailyDistribution") for p in posts))

        self.nvr.channels["2"] = ("Yard East", True)
        self.nvr.uptime = 300                         # Booted 5 minutes ago...
        saved = next((self.root / "reports").glob("nvr-01_*.json"))
        aged = json.loads(saved.read_text(encoding="utf-8"))
        aged["check_completed_at"] = (datetime.now(ICT) - timedelta(hours=2)).isoformat()
        saved.write_text(json.dumps(aged), encoding="utf-8")   # ...after a report 2 hours ago.
        self.nvr.requests.clear()
        second = self.run_report()
        by_ch = {c["channel_id"]: c for c in second["channel_inventory"]["channels"]}
        self.assertEqual(second["inventory_changes"]["changes"],
                         [{"type": "renamed", "channel_id": "2", "old": "Yard", "new": "Yard East"}])
        self.assertTrue(second["restart"]["detected"])
        self.assertEqual(by_ch["1"]["snapshot_result"]["status"], "PICTURE_FROZEN")  # Same bytes again.
        self.assertTrue(second["storage"]["smart"]["from_cache"])
        self.assertFalse(any("SMARTTest" in p for _, p in self.nvr.requests))      # SMART cached for 24 h.

        notes.add_known_issue("nvr-01", "3", "Lens covered by builders", date.today() + timedelta(days=3), "Heng")
        notes.add_note("nvr-01", "1", "Cleaned the dome", "Heng")
        data = dashboard.read_dashboard_data()
        nvr = data["nvrs"][0]
        cams = {c["channel_id"]: c for c in nvr["channels"]}
        self.assertEqual(cams["3"]["condition"]["code"], "KNOWN_ISSUE")
        self.assertEqual(cams["3"]["condition"]["original"], "BAD_PICTURE")
        self.assertEqual(cams["1"]["notes"][0]["text"], "Cleaned the dome")
        self.assertEqual(cams["1"]["condition"]["code"], "BAD_PICTURE")             # Frozen picture.
        self.assertTrue(cams["1"]["snapshot_result"]["has_image"])
        self.assertEqual(nvr["smart"]["status"], "WARNING")
        self.assertEqual(nvr["disks"][1]["smart"]["reallocated"], 8)
        self.assertTrue(nvr["restart"]["detected"])
        self.assertEqual(len(nvr["inventory_changes"]["changes"]), 1)
        self.assertEqual(data["summary"]["known_issues"], 1)

        # Email: the known issue is listed apart and not counted as needing action.
        email = camera_conditions(second, notes.load())
        self.assertEqual((email["known_issues"], email["bad_picture"]), (1, 2))  # ch1, ch2 frozen.
        self.assertTrue(any("renamed" in f for f in email["findings"]))

        # Marking ch1 fixed accepts its current view; the next picture is new again.
        notes.clear_known_issue("nvr-01", "1", "Heng", "replaced camera")
        self.nvr.picture_seed = 40
        third = self.run_report()
        by_ch = {c["channel_id"]: c for c in third["channel_inventory"]["channels"]}
        self.assertEqual(by_ch["1"]["snapshot_result"]["status"], "PICTURE_OK")

        # Bot: alerts and the picture command read the same data.
        calls = []

        class API:
            def call(self, method, http_timeout=15, **params):
                calls.append((method, params))
                return {"message_id": 1}

            def upload(self, method, field, filename, data, mime, **params):
                calls.append((method, {"filename": filename, "size": len(data), **params}))
                return {}
        settings = tb.load_settings({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ADMIN_USER_IDS": "111",
                                     "TELEGRAM_ALERT_CHAT_IDS": "-100900", "TELEGRAM_STATE_DIR": str(self.root / "tg")})
        bot = tb.Bot(settings, API())
        bot.addon_command(111, "snapshot", "nvr-01 1", "admin", "Heng (Telegram)")
        self.assertEqual(calls[-1][0], "sendPhoto")
        self.assertGreater(calls[-1][1]["size"], 1000)


class KnownIssueAndNotesTests(TempDirs):
    def test_parse_until(self):
        today = date(2026, 11, 4)
        self.assertEqual(notes.parse_until("7d", today), date(2026, 11, 11))
        self.assertEqual(notes.parse_until("2w", today), date(2026, 11, 18))
        self.assertEqual(notes.parse_until("2026-11-20", today), date(2026, 11, 20))
        self.assertIsNone(notes.parse_until("-", today))
        for bad in ("400d", "2025-01-01", "soon", "0d"):
            self.assertEqual(notes.parse_until(bad, today), "invalid", bad)

    def test_store_and_overlay(self):
        notes.add_known_issue("nvr-01", "5", "Cable <cut>\x00", date.today() + timedelta(days=2), "Heng")
        notes.add_known_issue("nvr-01", "5", "Replaced reason", None, "Heng")          # Replaces.
        notes.add_known_issue("nvr-02", None, "Building closed", date.today() - timedelta(days=1), "Heng")  # Expired.
        data = notes.load()
        self.assertEqual(len(data["known_issues"]), 2)
        self.assertEqual([i["nvr"] for i in notes.active_issues(data)], ["nvr-01"])
        rows = [{**channel(5, "NO_RECENT_RECORDING_EVIDENCE"), "condition": None},
                {**channel(6), "condition": None}]
        for row in rows:
            row["condition"] = classify(row)
        notes.overlay("nvr-01", rows, data, known_condition())
        self.assertEqual(rows[0]["condition"]["code"], "KNOWN_ISSUE")
        self.assertIn("Replaced reason", rows[0]["condition"]["detail"])
        self.assertEqual(rows[1]["condition"]["code"], "OK")
        self.assertTrue(notes.clear_known_issue("nvr-01", "5", "Heng", "spliced"))
        self.assertFalse(notes.active_issues(notes.load()))
        log = notes.load()["log"]
        self.assertEqual([n["kind"] for n in log], ["known", "known", "known", "fixed"])
        with self.assertRaises(ValueError):
            notes.add_note("../etc", "5", "x", "y")
        with self.assertRaises(ValueError):
            notes.add_note("nvr-01", "5/../x", "x", "y")

    def test_dashboard_notes_api_and_snapshot_route(self):
        (self.root / "snapshots" / "nvr-01").mkdir()
        (self.root / "snapshots" / "nvr-01" / "5.jpg").write_bytes(jpeg("scene"))
        with patch.object(dashboard, "DASHBOARD_USERNAME", "demo"), \
                patch.object(dashboard, "DASHBOARD_PASSWORD", "test-password-long"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            auth = {"Authorization": "Basic " + base64.b64encode(b"demo:test-password-long").decode()}

            def call(path, body=None, headers=None):
                data = json.dumps(body).encode() if body is not None else None
                try:
                    with urlopen(Request(base + path, data=data, method="POST" if data else "GET",
                                         headers={**auth, **(headers or {})}), timeout=3) as r:
                        return r.status, r.read()
                except HTTPError as exc:
                    return exc.code, exc.read()
            try:
                ok = {"Content-Type": "application/json", "X-NVR-Track": "notes"}
                body = {"action": "known", "nvr": "nvr-01", "channel": "5", "text": "Cable", "until": "3d"}
                self.assertEqual(call("/api/notes", body, {"Content-Type": "application/json"})[0], 403)
                self.assertEqual(call("/api/notes", body, {**ok, "X-NVR-Track": "scan"})[0], 403)
                self.assertEqual(call("/api/notes", body, ok)[0], 200)
                self.assertEqual(notes.active_issues(notes.load())[0]["reason"], "Cable")
                self.assertEqual(call("/api/notes", {**body, "until": "999d"}, ok)[0], 400)
                self.assertEqual(call("/api/notes", {**body, "nvr": "../x"}, ok)[0], 400)
                self.assertEqual(call("/api/notes", {"action": "fixed", "nvr": "nvr-01", "channel": "5"}, ok)[0], 200)
                self.assertEqual(notes.active_issues(notes.load()), [])
                status, image = call("/api/snapshot?nvr=nvr-01&ch=5")
                self.assertEqual((status, image[:2]), (200, b"\xff\xd8"))
                self.assertEqual(call("/api/snapshot?nvr=nvr-01&ch=../../etc/passwd")[0], 400)
                self.assertEqual(call("/api/snapshot?nvr=nvr-01&ch=6")[0], 404)
                self.assertEqual(urlopen_status(base + "/api/snapshot?nvr=nvr-01&ch=5"), 401)
            finally:
                server.shutdown()
                server.server_close()


def urlopen_status(url):
    try:
        with urlopen(url, timeout=3) as r:
            return r.status
    except HTTPError as exc:
        return exc.code


class InventoryAndRestartTests(unittest.TestCase):
    def test_changes(self):
        before = [{"channel_id": "1", "name": "Gate", "camera_ip": "10.0.0.1"},
                  {"channel_id": "2", "name": "Yard", "camera_ip": "10.0.0.2"},
                  {"channel_id": "3", "name": "Old", "camera_ip": "10.0.0.3"}]
        after = [{"channel_id": "1", "name": "Gate North", "camera_ip": "10.0.0.1"},
                 {"channel_id": "2", "name": "Yard", "camera_ip": "10.0.0.9"},
                 {"channel_id": "4", "name": "New", "camera_ip": "10.0.0.4"}]
        types = [(c["type"], c["channel_id"]) for c in compare_inventory(before, after)]
        self.assertEqual(types, [("added", "4"), ("removed", "3"), ("renamed", "1"), ("ip_changed", "2")])

    def test_unreachable_or_fallback_lists_are_never_compared(self):
        fallback = {"channel_inventory": {"status": "TIMEOUT", "channels": []}}
        good = {"check_completed_at": "x", "channel_inventory": {"status": "CHANNELS_RETRIEVED",
                                                                 "channels": [{"channel_id": "1"}]}}
        self.assertEqual(inventory_changes([fallback, good], None)["status"], "NOT_COMPARED")
        result = inventory_changes([fallback, good], [{"channel_id": "1"}])
        self.assertEqual((result["status"], result["changes"]), ("COMPARED", []))

    def test_restart(self):
        now = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
        previous = (now - timedelta(hours=2)).isoformat()
        self.assertTrue(restart_info(600, now, previous)["detected"])
        self.assertFalse(restart_info(86400, now, previous)["detected"])
        self.assertIsNone(restart_info(None, now, previous)["detected"])
        self.assertIsNone(restart_info(600, now, None)["detected"])


class SmartTests(unittest.TestCase):
    def smart_xml(self, **fields):
        attrs = "".join(f"<TestResult><attributeID>{k}</attributeID><status>{s}</status><rawValue>{r}</rawValue></TestResult>"
                        for k, (s, r) in fields.get("attrs", {}).items())
        return (f"<SMARTTestStatus><temprature>{fields.get('temp', 40)}</temprature>"
                f"<selfEvaluaingStatus>{fields.get('self_eval', 'ok')}</selfEvaluaingStatus>"
                f"<allEvaluaingStatus>{fields.get('all', 'ok')}</allEvaluaingStatus>"
                f"<TestResultList>{attrs}</TestResultList></SMARTTestStatus>").encode()

    def status(self, **fields):
        return smart.classify_disk(smart.parse_smart(self.smart_xml(**fields)))[0]

    def test_classification(self):
        self.assertEqual(self.status(), "HEALTHY")
        self.assertEqual(self.status(attrs={5: ("ok", 3)}), "WARNING")
        self.assertEqual(self.status(attrs={197: ("ok", 250)}), "CRITICAL")
        self.assertEqual(self.status(all="fault"), "CRITICAL")
        self.assertEqual(self.status(temp=57), "WARNING")
        self.assertEqual(self.status(temp=63), "CRITICAL")
        self.assertEqual(self.status(attrs={9: ("fail", 1)}), "WARNING")
        # Hikvision's own words for a healthy disk; unknown words never alarm.
        self.assertEqual(self.status(self_eval="pass", all="functional"), "HEALTHY")
        self.assertEqual(self.status(all="somethingnew"), "UNVERIFIED")
        self.assertEqual(self.status(self_eval="error"), "CRITICAL")
        self.assertEqual(self.status(all="warning"), "WARNING")
        self.assertEqual(self.status(attrs={190: ("pre-fail", 0), 9: ("old_age", 0)}), "HEALTHY")
        unknown = smart.parse_smart(b"<SMARTTestStatus><other>1</other></SMARTTestStatus>")
        self.assertEqual(smart.classify_disk(unknown)[0], "UNVERIFIED")
        with self.assertRaises(ValueError):
            smart.parse_smart(b"<ResponseStatus/>")

    def test_cache(self):
        recent = {"status": "HEALTHY", "checked_at": datetime.now(timezone.utc).isoformat(),
                  "disks": [{"id": "1"}, {"id": "2"}], "rules": smart.RULES_VERSION}
        self.assertTrue(smart.cached(recent, ["2", "1"], 24)["from_cache"])
        self.assertIsNone(smart.cached({**recent, "rules": None}, ["1", "2"], 24))  # Old rules: re-check.
        self.assertIsNone(smart.cached(recent, ["1"], 24))                       # Disk set changed.
        old = {**recent, "checked_at": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()}
        self.assertIsNone(smart.cached(old, ["1", "2"], 24))


class SnapshotJudgeTests(unittest.TestCase):
    def metrics(self, kind="scene", seed=0):
        return snapshots.analyze(jpeg(kind, seed))

    def test_black_flat_frozen_moved_and_reset(self):
        status, _, _ = snapshots.judge(self.metrics("black"), "a", None, None)
        self.assertEqual(status, "PICTURE_BLACK")
        flat = {"brightness": 120.0, "contrast": 0.5, "mode": "day", "dhash": "0" * 16}
        self.assertEqual(snapshots.judge(flat, "b", None, None)[0], "PICTURE_UNIFORM")
        status, _, entry = snapshots.judge(self.metrics(), "c", None, None)
        self.assertEqual(status, "PICTURE_OK")                                  # Reference saved.
        self.assertEqual(snapshots.judge(self.metrics(), "c", entry, None)[0], "PICTURE_FROZEN")
        moved = snapshots.analyze(self._flipped())
        status, _, moved_entry = snapshots.judge(moved, "d", entry, None)
        self.assertEqual(status, "PICTURE_VIEW_CHANGED")
        later = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        self.assertEqual(snapshots.judge(moved, "e", moved_entry, later)[0], "PICTURE_OK")  # Marked fixed.

    def _flipped(self):
        from PIL import Image
        image = Image.open(io.BytesIO(jpeg("scene"))).transpose(Image.FLIP_LEFT_RIGHT)
        out = io.BytesIO()
        image.save(out, "JPEG")
        return out.getvalue()

    def test_bad_picture_condition_only_when_recording(self):
        cam = {**channel(1), "snapshot_result": {"status": "PICTURE_BLACK"}}
        self.assertEqual(classify(cam)["code"], "BAD_PICTURE")
        stopped = {**channel(1, "NO_RECENT_RECORDING_EVIDENCE"), "snapshot_result": {"status": "PICTURE_BLACK"}}
        self.assertEqual(classify(stopped)["code"], "NOT_RECORDING")


class EventStreamTests(TempDirs):
    def test_parser_handles_split_chunks_and_noise(self):
        stream = ("--b\r\nContent-Type: application/xml\r\n\r\n" + event_xml("videoloss", "0", "inactive", 0)
                  + "\r\n--b\r\n\r\n" + event_xml("VMD", "2") + "--b\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8junk"
                  + "\r\n--b\r\n" + event_xml("shelteralarm", "3")).encode("latin-1")
        parser = events.StreamParser()
        found = []
        for i in range(0, len(stream), 37):
            found += parser.feed(stream[i:i + 37])
        self.assertEqual([e["type"] for e in found], ["videoloss", "vmd", "shelteralarm"])
        self.assertTrue(events.is_heartbeat(found[0]))
        self.assertEqual(found[2]["channel"], "3")

    def test_dedupe_record_and_read_new(self):
        dedupe = events.Deduplicator(30)
        event = {"type": "videoloss", "state": "active", "channel": "2", "count": 1,
                 "device_time": "", "description": ""}
        now = datetime.now(timezone.utc)
        self.assertTrue(dedupe.accept("nvr-01", event, now))
        self.assertFalse(dedupe.accept("nvr-01", event, now + timedelta(minutes=5)))
        self.assertTrue(dedupe.accept("nvr-01", event, now + timedelta(minutes=31)))
        self.assertFalse(dedupe.accept("nvr-01", {**event, "state": "inactive"}, now))
        _, position = events.read_new({})
        events.record("nvr-01", event, events.EVENT_TYPES)
        rows, position = events.read_new(position)
        self.assertEqual([(r["label"], r["channel"]) for r in rows], [("Video loss", "2")])
        self.assertEqual(events.read_new(position)[0], [])
        self.assertEqual(len(events.recent_events()), 1)

    def test_listener_against_fake_nvr(self):
        nvr = FakeNVR()
        self.addCleanup(nvr.close)
        nvr.events = [event_xml("videoloss", "0", "inactive", 0), event_xml("VMD", "1"),
                      event_xml("diskerror", "0"), event_xml("videoloss", "2"), event_xml("videoloss", "2", count=2)]

        class Inventory:
            id, ip, http_port = "nvr-01", "127.0.0.1", nvr.port
        listener = event_listener.Listener(Inventory, events.wanted_types(), events.Deduplicator(30),
                                           threading.Lock())

        class Stop(Exception):
            pass

        def stop(_seconds):
            raise Stop
        with patch.dict("os.environ", {"NVR_USERNAME": "admin", "NVR_PASSWORD": "x"}), \
                patch.object(event_listener.time, "sleep", stop), patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(Stop):
                listener.run()
        rows = events.recent_events()
        self.assertEqual(sorted(r["type"] for r in rows), ["diskerror", "videoloss"])
        self.assertEqual(listener.state["ignored_types"], {"vmd": 1})
        self.assertEqual(listener.state["last_error"], "STREAM_CLOSED")
        self.assertEqual({m for m, _ in nvr.requests}, {"GET"})


class AddonBotTests(Fixture):
    def setUp(self):
        super().setUp()
        for name in ("notes", "events", "snapshots"):
            (self.root / name).mkdir(exist_ok=True)
        env = patch.dict("os.environ", {"NOTES_DIR": str(self.root / "notes"),
                                        "EVENTS_DIR": str(self.root / "events")})
        env.start()
        self.addCleanup(env.stop)
        self.uploads = []
        self.api.upload = lambda method, field, filename, data, mime, **p: self.uploads.append((method, filename)) or {}

    def alerts(self):
        self.calls.clear()
        self.bot.check_alerts()
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def test_known_issue_pauses_alerts_until_fixed(self):
        self.report(channels=[channel(1), channel(2)])
        self.alerts()
        self.assertIn("Only administrators", self.message(222, "/known nvr-01 2 7d cable")[0]["text"])
        self.assertIn("Known issue saved", self.message(111, "/known nvr-01 2 3d Cable cut, Friday")[0]["text"])
        self.report(channels=[channel(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE")])
        self.assertEqual(self.alerts(), [])                                    # Paused.
        listing = self.message(222, "/known")[0]["text"]
        self.assertIn("Cable cut, Friday", listing)
        self.assertIn("Known issues", self.message(222, "/problems")[0]["text"])
        self.assertIn("marked fixed", self.message(111, "/fixed nvr-01 2 spliced")[0]["text"])
        text = self.alerts()[0]                                                # Still broken: alerts again.
        self.assertIn("Stopped recording", text)
        self.assertIn("Went offline", text)
        notes_text = self.message(222, "/notes nvr-01 2")[0]["text"]
        self.assertIn("Fixed: spliced", notes_text)

    def test_marking_an_alerted_camera_known_sends_no_false_recovery(self):
        self.report(channels=[channel(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE")])
        self.alerts()                                                          # Hello (baseline).
        self.report(channels=[channel(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE")],
                    storage="CRITICAL")
        self.alerts()
        self.message(111, "/known nvr-01 2 3d Cable cut")
        self.assertEqual(self.alerts(), [])                                    # No "Recording again".

    def test_one_off_entries_are_never_resolved(self):
        from app import telegram_views as views
        previous = {"change:nvr-01:t:0": {"text": "added", "resolved": "", "severity": "warning",
                                           "value": "", "event": True},
                    "cam:nvr-01:5:rec": {"text": "stopped", "resolved": "again", "severity": "critical", "value": ""}}
        merged, opened, resolved = views.compare(previous, {}, [], {"nvr-01"}, [])
        self.assertEqual([e["resolved"] for e in resolved], ["again"])
        merged, opened, resolved = views.compare(previous, {}, [], {"nvr-01"}, ["cam:nvr-01:5:"])
        self.assertEqual((merged, resolved), ({}, []))

    def test_several_channels_at_once(self):
        self.report(channels=[channel(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE"),
                              channel(3, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE")])
        # One reply for all of them (a message per camera hit Telegram's flood limits).
        replies = [p["text"] for p in self.message(111, "/known nvr-01 2,ch3 - Cameras removed")]
        self.assertEqual(len(replies), 1)
        self.assertIn("ch2, ch3", replies[0])
        self.assertIn("until /fixed", replies[0])
        listing = self.message(222, "/known")[0]["text"]
        self.assertIn("ch2", listing)
        self.assertIn("ch3", listing)
        self.assertEqual({i["reason"] for i in notes.active_issues(notes.load())}, {"Cameras removed"})
        self.assertIn("no channel 9", self.message(111, "/known nvr-01 2,9 - x")[0]["text"])
        fixed = self.message(111, "/fixed nvr-01 2,3,1")
        self.assertEqual(len(fixed), 1)
        self.assertIn("2 of 3 had a known issue", fixed[0]["text"])
        self.assertEqual(notes.active_issues(notes.load()), [])
        noted = self.message(111, "/note nvr-01 1,2 Cleaned lenses")
        self.assertEqual((len(noted), "Note saved for" in noted[0]["text"]), (1, True))
        self.assertEqual(sum(n["text"] == "Cleaned lenses" for n in notes.load()["log"]), 2)

    def test_uptime_with_an_invalid_month_answers_with_usage(self):
        self.report(channels=[channel(1)])
        for text in ("/uptime 2026-13", "/uptime 2026-00", "/uptime 1999-05"):
            sent = self.message(222, text)
            self.assertEqual(len(sent), 1, text)
            self.assertIn("/uptime 2026-10", sent[0]["text"])

    def test_whole_nvr_known_issue_and_notes_permissions(self):
        self.report(channels=[channel(1)])
        self.alerts()
        self.message(111, "/known nvr-01 all - Building closed")
        self.report(channels=[channel(1, "NO_RECENT_RECORDING_EVIDENCE")], storage="CRITICAL")
        self.assertEqual(self.alerts(), [])
        self.assertIn("Only administrators", self.message(222, "/note nvr-01 1 hello")[0]["text"])
        self.bot.settings["notes_by"] = "users"
        self.assertIn("Note saved", self.message(222, "/note nvr-01 1 Re-aimed after storm")[0]["text"])
        self.assertIn("Re-aimed after storm", self.message(222, "/camera nvr-01 1")[0]["text"])
        self.assertIn("Which camera", self.message(111, "/known nvr-01 7d x")[0]["text"])

    def test_change_restart_picture_and_stream_alerts(self):
        self.report(channels=[channel(1), channel(2)])
        self.alerts()
        path = next((self.root / "reports").glob("nvr-01_*.json"))
        report = json.loads(path.read_text())
        report["inventory_changes"] = {"status": "COMPARED", "changes": [
            {"type": "added", "channel_id": "2", "new": "Yard"}]}
        report["restart"] = {"detected": True, "boot_time": datetime.now(timezone.utc).isoformat()}
        report["channel_inventory"]["channels"][0]["snapshot_result"] = {"status": "PICTURE_UNIFORM", "message": "flat"}
        path.write_text(json.dumps(report))
        (self.root / "events" / "status.json").write_text(json.dumps({
            "schema_version": 1, "updated_at": datetime.now(timezone.utc).isoformat(),
            "nvrs": {"nvr-01": {"connected": False, "last_error": "ConnectTimeout"}}}))
        text = self.alerts()[0]
        for expected in ("Camera added", "restarted", "Picture flat", "event stream disconnected"):
            self.assertIn(expected, text)
        self.assertEqual(self.alerts(), [])                                     # Announced once.
        self.report(channels=[{**channel(1), "snapshot_result": {"status": "PICTURE_OK"}}, channel(2)])
        text = self.alerts()                                                    # Next report: events gone.
        self.assertTrue(all("Camera added" not in t and "restarted" not in t for t in text))
        self.assertTrue(any("Picture OK again" in t for t in text))

    def test_realtime_events_are_posted_once_and_respect_known_issues(self):
        self.report(channels=[channel(1), channel(2)])
        event = {"type": "videoloss", "state": "active", "channel": "1", "count": 1, "device_time": "", "description": ""}
        events.record("nvr-01", event, events.EVENT_TYPES)
        self.calls.clear()
        self.bot.check_events()                                                 # First start: old events skipped.
        self.assertEqual(self.calls, [])
        events.record("nvr-01", {**event, "channel": "2"}, events.EVENT_TYPES)
        notes.add_known_issue("nvr-01", "1", "x", None, "t")
        events.record("nvr-01", event, events.EVENT_TYPES)
        self.bot.check_events()
        sent = [p["text"] for m, p in self.calls if m == "sendMessage"]
        self.assertEqual(len(sent), 1)
        self.assertIn("ch2 Cam 2", sent[0])
        self.assertNotIn("ch1", sent[0])
        self.calls.clear()
        self.bot.check_events()
        self.assertEqual(self.calls, [])
        self.assertIn("Video loss", self.message(222, "/events")[0]["text"])

    def test_snapshot_and_uptime_commands(self):
        self.report(channels=[channel(1)])
        self.assertIn("Only administrators", self.message(222, "/snapshot nvr-01 1")[0]["text"])
        self.assertIn("No stored picture", self.message(111, "/snapshot nvr-01 1")[0]["text"])
        sent = self.message(222, "/uptime 7")
        self.assertIn("Calculating", sent[0]["text"])
        self.assertIn("Uptime last 7 days", sent[1]["text"])
        self.assertEqual(self.uploads[-1][0], "sendDocument")


class UptimeTests(TempDirs):
    def write(self, moment, statuses):
        channels = [{"channel_id": str(i + 1), "name": f"Cam {i + 1}", "reported_status": live,
                     "recording_result": {"status": rec}} for i, (live, rec) in enumerate(statuses)]
        report = {"nvr": {"id": "nvr-01", "name": "Kiln"}, "device_info": {"status": "DEVICE_INFO_RETRIEVED"},
                  "channel_inventory": {"channels": channels}}
        stamp = moment.strftime("%Y%m%dT%H%M%S%f%z")
        (self.root / "reports" / f"nvr-01_{stamp}.json").write_text(json.dumps(report))

    def test_percentages_and_flaps(self):
        start = datetime(2026, 10, 1, tzinfo=ICT)
        pattern = [("ONLINE", "RECORDING"), ("OFFLINE", "NO_RECENT_RECORDING_EVIDENCE"),
                   ("ONLINE", "RECORDING"), ("ONLINE", "CHECK_ERROR")]
        for i, cam1 in enumerate(pattern):
            self.write(start + timedelta(hours=2 * i), [cam1, ("ONLINE", "NOT_CONFIGURED")])
        self.write(datetime(2026, 11, 2, tzinfo=ICT), [("OFFLINE", "NO_RECENT_RECORDING_EVIDENCE")])  # Outside.
        result = uptime.compute(self.root / "reports", start, datetime(2026, 11, 1, tzinfo=ICT))
        cam1, cam2 = result["cameras"]
        self.assertEqual((cam1["samples"], cam1["online_pct"], cam1["recording_pct"], cam1["flaps"]),
                         (4, 75.0, 66.7, 2))
        self.assertIsNone(cam2["recording_pct"])                                # Never checked: unknown.
        self.assertEqual(result["nvrs"][0]["reachable_pct"], 100.0)
        self.assertIn("Cam 1", uptime.to_csv(result))

    def test_csv_formulas_are_inert_and_report_script_runs(self):
        self.write(datetime(2026, 10, 3, tzinfo=ICT), [("ONLINE", "RECORDING")])
        result = uptime.compute(self.root / "reports", datetime(2026, 10, 1, tzinfo=ICT), datetime(2026, 11, 1, tzinfo=ICT))
        result["cameras"][0]["name"] = "=HYPERLINK(1)"
        self.assertIn("'=HYPERLINK", uptime.to_csv(result))
        with patch.object(uptime_report, "REPORTS_DIR", self.root / "reports"), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(uptime_report.main(["--month", "2026-10"]), 0)
        self.assertTrue((self.root / "reports" / "uptime" / "uptime-2026-10.csv").exists())
        self.assertIn("recording 100.0%", out.getvalue())
        self.assertEqual(uptime_report.previous_month(datetime(2026, 1, 1, tzinfo=ICT)), "2025-12")


class ScannerAddonTests(TempDirs):
    def test_monthly_uptime_and_archive_run_once(self):
        service = scan_worker.Service()
        runs = []

        def fake_run(command, **kwargs):
            runs.append(command[2])
            class Result:
                returncode, stdout = 0, "Email report: SENT to 1\n"
            return Result
        first = datetime(2026, 12, 1, 8, 5, tzinfo=scan_worker.TZ)
        with patch.object(scan_worker, "STATE_DIR", self.root / "scan"), \
                patch.object(scan_worker.subprocess, "run", fake_run), \
                patch.dict("os.environ", {"NVR_REPORT_ARCHIVE_DAYS": "60"}), \
                patch.object(scan_worker, "datetime") as clock, patch("sys.stdout", new_callable=io.StringIO):
            clock.now.side_effect = lambda tz=None: first
            for _ in range(2):
                service.maybe_uptime_report()
                service.maybe_archive()
        self.assertEqual(runs, ["scripts.uptime_report", "scripts.archive_reports"])


if __name__ == "__main__":
    unittest.main()
