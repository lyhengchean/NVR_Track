"""Telegram bot, alerts, required-retention rule and report archiving. No network."""
import json
import tarfile
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import dashboard
from app import telegram_views as views
from app.camera_condition import classify
from scripts import archive_reports
from scripts import telegram_bot as tb

ICT = timezone(timedelta(hours=7))
TOKEN = "123456789:" + "A" * 35


def calendar(days):
    today = date.today()
    rows = [{"date": (today - timedelta(days=30 - i)).isoformat(), "has_recording": i >= 30 - days}
            for i in range(30)]
    return {"status": "RETENTION_VERIFIED", "window_days": 30, "days_with_footage": days,
            "calendar": rows, "as_of_date": today.isoformat()}


def channel(cid, status="RECORDING", live="ONLINE", name=None, days=30):
    return {"channel_id": str(cid), "name": name or f"Cam {cid}", "camera_ip": f"192.0.2.{cid}",
            "reported_status": live, "recording_expectation": "continuous",
            "recording_result": {"status": status, "window_minutes": 60},
            "historical_recording_result": {"status": "HISTORICAL_RECORDING_FOUND"
                                            if status != "RECORDING" else "HISTORICAL_NOT_CHECKED"},
            "retention_result": calendar(days)}


class Fixture(unittest.TestCase):
    """Real dashboard data from temporary reports, and a recording fake API."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "reports").mkdir()
        (self.root / "scan").mkdir()
        for target, value in (("REPORTS_DIR", self.root / "reports"), ("SCAN_STATE_DIR", self.root / "scan")):
            patcher = patch.object(dashboard, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.scanner(available=True)
        self.calls = []
        test = self

        class FakeAPI:
            def call(self, method, http_timeout=15, **params):
                test.calls.append((method, params))
                return {"message_id": 7}
        self.api = FakeAPI()
        self.settings = tb.load_settings({
            "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ADMIN_USER_IDS": "111",
            "TELEGRAM_ALLOWED_USER_IDS": "222", "TELEGRAM_ALLOWED_CHAT_IDS": "-100500",
            "TELEGRAM_ALERT_CHAT_IDS": "-100900", "TELEGRAM_STATE_DIR": str(self.root / "tg"),
            "TELEGRAM_DIGEST_TIME": "08:30"})
        self.bot = tb.Bot(self.settings, self.api)

    def scanner(self, available=True, **extra):
        status = {"schema_version": 1, "on_demand_enabled": True, "scan_times": ["08:00"],
                  "email_times": ["08:00"], "current": None, "last": None,
                  "heartbeat_at": (datetime.now(timezone.utc) - timedelta(minutes=0 if available else 10)).isoformat(),
                  **extra}
        (self.root / "scan" / "status.json").write_text(json.dumps(status), encoding="utf-8")

    def report(self, nvr_id="nvr-01", channels=(), name="Kiln <Block>", storage="HEALTHY",
               overall="WARNING", offset=0, minutes_ago=5, critical=()):
        done = datetime.now(ICT) - timedelta(minutes=minutes_ago)
        data = {"schema_version": 2, "nvr": {"id": nvr_id, "name": name, "ip": "10.9.9.1"},
                "check_completed_at": done.isoformat(),
                "device_info": {"status": "DEVICE_INFO_RETRIEVED", "model": "DS-8664NI-I8", "firmware_version": "V4.1"},
                "channel_inventory": {"expected_count": len(channels), "configured_count": len(channels),
                                      "channels": list(channels)},
                "channel_status": {"online_count": sum(c["reported_status"] == "ONLINE" for c in channels),
                                   "offline_count": sum(c["reported_status"] == "OFFLINE" for c in channels)},
                "storage": {"status": storage, "message": "HDD 3 error" if storage != "HEALTHY" else "", "disks": []},
                "per_camera_recording": {"enabled": True, "window_minutes": 60,
                                         "nvr_clock_offset_seconds_approx": offset},
                "overall": {"status": overall, "critical_reasons": list(critical)}}
        for old in (self.root / "reports").glob(f"{nvr_id}_*.json"):
            old.unlink()
        stamp = done.strftime("%Y%m%dT%H%M%S%f%z")
        (self.root / "reports" / f"{nvr_id}_{stamp}.json").write_text(json.dumps(data), encoding="utf-8")

    def message(self, user, text, chat=None, chat_type="private", age=0):
        self.calls.clear()
        self.bot.handle_update({"update_id": 1, "message": {
            "date": int(time.time()) - age, "text": text, "from": {"id": user},
            "chat": {"id": chat if chat is not None else user, "type": chat_type}}})
        return [params for method, params in self.calls if method == "sendMessage"]

    def press(self, user, data, chat=None):
        self.calls.clear()
        self.bot.handle_callback({"id": "q", "data": data, "from": {"id": user},
                                  "message": {"message_id": 5, "chat": {"id": chat or user}}})
        return self.calls


class SettingsAndAccessTests(Fixture):
    def test_settings(self):
        s = tb.load_settings({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ADMIN_USER_IDS": "1, 2",
                              "TELEGRAM_ALLOWED_USER_IDS": "3;x;4", "TELEGRAM_ALERT_CHAT_IDS": "-100",
                              "TELEGRAM_DIGEST_TIME": "25:00"})
        self.assertEqual((s["admins"], s["users"], s["chats"]), ({1, 2}, {1, 2, 3, 4}, {-100}))
        self.assertIsNone(s["digest"])
        self.assertFalse(s["show_ips"])
        self.assertEqual(tb.load_settings({"TELEGRAM_BOT_TOKEN": "not-a-token"})["token"], "")

    def test_roles(self):
        self.assertEqual(tb.role(self.settings, 111, 111), "admin")
        self.assertEqual(tb.role(self.settings, 222, 222), "viewer")
        self.assertEqual(tb.role(self.settings, 333, -100500), "viewer")   # Member of an allowed group.
        self.assertEqual(tb.role(self.settings, 333, -100900), "viewer")   # Alert group.
        self.assertIsNone(tb.role(self.settings, 333, 333))

    def test_strangers_learn_only_their_id_once(self):
        self.report(channels=[channel(1)])
        sent = self.message(999, "/status")
        self.assertEqual(len(sent), 1)
        self.assertIn("This bot is private", sent[0]["text"])
        self.assertIn("<code>999</code>", sent[0]["text"])
        self.assertNotIn("nvr-01", sent[0]["text"])
        self.assertEqual(self.message(999, "/status"), [])                       # Throttled.
        self.assertEqual(self.message(998, "/status", chat=-555, chat_type="group"), [])  # Unknown group.

    def test_old_and_foreign_commands_are_ignored(self):
        self.report(channels=[channel(1)])
        self.assertEqual(self.message(222, "/status", age=3600), [])
        self.bot.username = "nvr_track_bot"
        self.assertEqual(self.message(222, "/status@other_bot", chat=-100500, chat_type="group"), [])
        self.assertEqual(len(self.message(222, "/status@nvr_track_bot", chat=-100500, chat_type="group")), 1)
        self.assertEqual(self.message(222, "hello", chat=-100500, chat_type="group"), [])


class ViewTests(Fixture):
    def setUp(self):
        super().setUp()
        self.report(channels=[channel(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE", name="Gate <x>"),
                              channel(3, live="OFFLINE")])

    def test_status_and_problems_escape_device_text(self):
        status = self.message(222, "/status")[0]
        self.assertIn("1 camera is not recording", status["text"])
        self.assertEqual(status["parse_mode"], "HTML")
        self.assertIn("inline_keyboard", status["reply_markup"])
        problems = self.message(222, "/problems")[0]["text"]
        self.assertIn("Gate &lt;x&gt;", problems)
        self.assertIn("Kiln &lt;Block&gt;", problems)
        self.assertNotIn("<x>", problems)

    def test_nvr_camera_offline_storage_firmware(self):
        detail = self.message(222, "/nvr 1")[0]
        self.assertIn("Kiln &lt;Block&gt; (nvr-01)", detail["text"])
        self.assertNotIn("10.9.9.1", detail["text"])                           # IPs hidden by default.
        callbacks = [b["callback_data"] for row in detail["reply_markup"]["inline_keyboard"] for b in row]
        self.assertNotIn("scan:nvr-01", callbacks)                             # Viewers get no scan button.
        self.assertIn("ch2 Gate &lt;x&gt;", self.message(222, "gate")[0]["text"])
        self.assertIn("ch2", self.message(222, "/camera nvr-01 2")[0]["text"])
        self.assertNotIn("192.0.2.2", self.message(222, "/camera 192.0.2.2")[0]["text"])
        self.assertIn("ch3 Cam 3 (still recording)", self.message(222, "/offline")[0]["text"])
        self.assertIn("Healthy", self.message(222, "/storage")[0]["text"])
        self.assertIn("DS-8664NI-I8 · V4.1", self.message(222, "/firmware")[0]["text"])
        self.assertIn("No NVR called", self.message(222, "/nvr nowhere")[0]["text"])

    def test_buttons_edit_the_message(self):
        calls = self.press(222, "nvr:nvr-01")
        self.assertEqual([m for m, _ in calls], ["answerCallbackQuery", "editMessageText"])
        self.assertEqual(calls[1][1]["message_id"], 5)

    def test_long_messages_are_split(self):
        parts = views.split_message("\n".join(f"line {i} " + "x" * 80 for i in range(200)))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= views.MESSAGE_LIMIT for p in parts))


class ScanTests(Fixture):
    def setUp(self):
        super().setUp()
        self.report(channels=[channel(1)])

    def test_only_admins_scan_and_requester_is_told_when_done(self):
        self.assertIn("Only administrators", self.message(222, "/scan nvr-01")[0]["text"])
        self.assertFalse((self.root / "scan" / "request.json").exists())
        self.assertIn("Only administrators", [p["text"] for m, p in self.press(222, "scanok:all")
                                              if m == "sendMessage"][0])
        sent = self.message(111, "/scan 1")
        self.assertIn("Scan of nvr-01 requested", sent[0]["text"])
        request = json.loads((self.root / "scan" / "request.json").read_text())
        self.assertEqual(request["target"], "nvr-01")
        later = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        self.scanner(last={"target": "nvr-01", "result": "OK", "overall": "WARNING", "finished_at": later})
        self.calls.clear()
        self.bot.check_pending_scans()
        self.assertIn("Scan of nvr-01 finished: Warning", self.calls[0][1]["text"])
        self.calls.clear()
        self.bot.check_pending_scans()
        self.assertEqual(self.calls, [])

    def test_scan_button_asks_first_and_busy_scanner_is_explained(self):
        calls = self.press(111, "scan:all")
        self.assertIn("Scan all NVRs now?", calls[-1][1]["text"])
        self.assertFalse((self.root / "scan" / "request.json").exists())
        self.scanner(current={"target": "all", "progress": {"done": 1, "total": 12}})
        self.press(111, "scanok:all")
        self.assertIn("already running", self.calls[-1][1]["text"])


class AlertTests(Fixture):
    def alerts(self):
        self.calls.clear()
        self.bot.check_alerts()
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def test_change_based_alerts(self):
        self.report(channels=[channel(1), channel(2)])
        first = self.alerts()
        self.assertEqual(len(first), 1)
        self.assertIn("bot started", first[0])                    # One hello, no alert flood.
        self.assertEqual(self.alerts(), [])                       # Nothing changed.

        self.report(channels=[channel(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE")],
                    storage="CRITICAL")
        text = self.alerts()[0]
        self.assertIn("Stopped recording", text)
        self.assertIn("Went offline", text)
        self.assertIn("storage critical", text)

        # A failed check is unknown: no false "recording again".
        self.report(channels=[channel(1), channel(2, "CHECK_ERROR", live="UNKNOWN")], storage="CRITICAL")
        text = self.alerts()
        self.assertTrue(all("Recording again" not in t and "Back online" not in t for t in text))

        self.report(channels=[channel(1), channel(2)])
        text = self.alerts()[0]
        for expected in ("Recording again", "Back online", "storage healthy again"):
            self.assertIn(expected, text)

    def test_unreachable_nvr_and_scanner_alerts(self):
        self.report(channels=[channel(1, "NO_RECENT_RECORDING_EVIDENCE")])
        self.alerts()
        # The NVR could not be scanned: its cameras keep their last state.
        manifest = {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "devices": [{"id": "nvr-01", "result": "ERROR", "reason": "PROCESS_TIMEOUT"}]}
        (self.root / "reports" / "fleet_latest_run.json").write_text(json.dumps(manifest))
        self.scanner(available=False)
        text = self.alerts()[0]
        self.assertIn("could not be scanned", text)
        self.assertIn("Scanner stopped", text)
        self.assertNotIn("Recording again", text)

    def test_clock_drift_and_quiet_telegram(self):
        self.report(channels=[channel(1)])
        self.alerts()
        self.report(channels=[channel(1)], offset=600)
        self.assertIn("clock is 600 s ahead", self.alerts()[0])

        # Telegram unreachable: state is not saved, so the alert is retried.
        self.report(channels=[channel(1, "NO_RECENT_RECORDING_EVIDENCE")], offset=600)

        def down(method, **params):
            raise tb.TelegramError("sendMessage: ConnectionError")
        with patch.object(self.api, "call", side_effect=down):
            self.bot.check_alerts()
        self.assertIn("Stopped recording", self.alerts()[0])

    def test_subscriptions_and_daily_digest(self):
        self.report(channels=[channel(1)])
        self.message(222, "/alerts on")
        self.assertIn(222, self.bot.recipients())
        self.message(222, "/alerts off")
        self.assertNotIn(222, self.bot.recipients())
        self.assertIn(-100900, self.bot.recipients())
        fixed = datetime.now(views.LOCAL_TZ).replace(hour=9, minute=0)
        with patch.object(tb, "datetime") as clock:
            clock.now.side_effect = lambda tz=None: fixed if tz else fixed.astimezone(timezone.utc)
            clock.fromisoformat = datetime.fromisoformat
            self.calls.clear()
            self.bot.check_digest()
            self.bot.check_digest()
        digests = [p for m, p in self.calls if m == "sendMessage"]
        self.assertEqual(len(digests), 1)
        self.assertIn("Daily summary", digests[0]["text"])


class AccessModeTests(Fixture):
    """No IDs to collect: TELEGRAM_ACCESS=open or group. Scans stay admin-only."""

    def use(self, **env):
        base = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ADMIN_USER_IDS": "111",
                "TELEGRAM_SHOW_IPS": "true", "TELEGRAM_STATE_DIR": str(self.root / "tg")}
        self.settings = tb.load_settings({**base, **env})
        self.bot = tb.Bot(self.settings, self.api)

    def setUp(self):
        super().setUp()
        self.report(channels=[channel(1), channel(2, "NO_RECENT_RECORDING_EVIDENCE")])

    def test_open_mode_gives_everyone_viewer_rights_only(self):
        self.use(TELEGRAM_ACCESS="open", TELEGRAM_BLOCKED_USER_IDS="666")
        self.assertIn("not recording", self.message(4242, "/status")[0]["text"])
        self.assertIn("Only administrators", self.message(4242, "/scan all")[0]["text"])
        self.assertFalse((self.root / "scan" / "request.json").exists())
        self.assertNotIn("192.0.2.2", self.message(4242, "/camera 2")[0]["text"])  # No IPs for viewers.
        self.assertIn("192.0.2.2", self.message(111, "/camera 2")[0]["text"])      # Admin sees them.
        self.assertIn("This bot is private", self.message(666, "/status")[0]["text"])  # Blocked.

    def test_group_mode_checks_membership_with_telegram_and_caches_it(self):
        self.use(TELEGRAM_ACCESS="group", TELEGRAM_ALLOWED_CHAT_IDS="-100777")
        members = {5001: "member", 5002: "left", 5003: "kicked"}
        asked = []
        test = self

        class API:
            def call(self, method, http_timeout=15, **params):
                test.calls.append((method, params))
                if method == "getChatMember":
                    asked.append(params["user_id"])
                    return {"status": members[params["user_id"]]}
                return {"message_id": 7}
        self.bot.api = API()
        self.assertIn("not recording", self.message(5001, "/status")[0]["text"])
        self.message(5001, "/problems")
        self.assertEqual(asked, [5001])                                   # Cached.
        refused = self.message(5002, "/status")[0]["text"]
        self.assertIn("staff group", refused)
        self.assertIn("This bot is private", self.message(5003, "/status")[0]["text"])  # Removed member.
        self.assertEqual(self.message(5003, "/status"), [])               # Throttled refusal.
        self.assertIn("Only administrators", self.message(5001, "/scan all")[0]["text"])

    def test_allowlist_is_still_the_default(self):
        self.use()
        self.assertEqual(self.settings["access"], "allowlist")
        self.assertIn("This bot is private", self.message(4242, "/status")[0]["text"])
        self.assertEqual(tb.load_settings({"TELEGRAM_ACCESS": "everyone"})["access"], "allowlist")

    def test_flooding_is_rate_limited_for_viewers_not_admins(self):
        self.use(TELEGRAM_ACCESS="open")
        replies = sum(len(self.message(4242, "/status")) for _ in range(30))
        self.assertEqual(replies, tb.RATE_LIMIT[0])
        self.assertEqual(sum(len(self.message(111, "/status")) for _ in range(30)), 30)


class TokenSafetyTests(unittest.TestCase):
    def test_network_errors_never_contain_the_token(self):
        import requests

        class Broken:
            def post(self, url, **kwargs):
                raise requests.ConnectionError(f"failed to reach {url}")
        api = tb.TelegramAPI(TOKEN, session=Broken())
        with self.assertRaises(tb.TelegramError) as ctx:
            api.call("getMe")
        self.assertNotIn(TOKEN.split(":")[1], str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_compose_service_gets_no_nvr_or_smtp_secrets(self):
        text = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
        block = text.split("  nvr-telegram:", 1)[1].split("\n  nvr-", 1)[0]  # This service only.
        self.assertNotIn("env_file", block)
        self.assertNotIn("PASSWORD", block)
        self.assertNotIn("nvrs.local.json", block)
        self.assertIn("./reports:/app/reports:ro", block)


class RequiredRetentionTests(unittest.TestCase):
    def test_short_history_becomes_a_warning_when_days_are_required(self):
        cam = channel(1, days=10)
        self.assertEqual(classify(cam)["code"], "SHORT_HISTORY")
        with patch.dict("os.environ", {"NVR_REQUIRED_RETENTION_DAYS": "14"}):
            result = classify(cam)
            self.assertEqual((result["code"], result["severity"]), ("BELOW_RETENTION", "warning"))
            self.assertIn("14 days are required", result["detail"])
            self.assertEqual(classify(channel(1, days=20))["code"], "SHORT_HISTORY")
        with patch.dict("os.environ", {"NVR_REQUIRED_RETENTION_DAYS": "nonsense"}):
            self.assertEqual(classify(cam)["code"], "SHORT_HISTORY")
        self.assertIn('"BELOW_RETENTION"', dashboard.HTML)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.now = datetime(2026, 11, 3, 8, 0, tzinfo=ICT)

    def make(self, nvr_id, days_ago):
        moment = self.now - timedelta(days=days_ago)
        path = self.dir / f"{nvr_id}_{moment.strftime('%Y%m%dT%H%M%S%f%z')}.json"
        path.write_text(json.dumps({"nvr": {"id": nvr_id}, "n": days_ago}), encoding="utf-8")
        return path

    def test_selects_only_old_reports_beyond_the_newest_kept(self):
        old = [self.make("nvr-01", d) for d in range(80, 100)]
        keep = [self.make("nvr-01", d) for d in range(0, 5)]
        lonely_old = self.make("nvr-02", 200)               # Only report of nvr-02: kept.
        (self.dir / "fleet_latest_run.json").write_text("{}")
        months = archive_reports.select(self.dir, 60, keep_per_nvr=5, now=self.now)
        chosen = {p for paths in months.values() for p in paths}
        self.assertEqual(chosen, set(old))
        self.assertFalse(chosen & set(keep))
        self.assertNotIn(lonely_old, chosen)

    def test_archive_is_verified_before_originals_are_removed(self):
        paths = [self.make("nvr-01", d) for d in (90, 91)]
        target = archive_reports.archive_month(self.dir, "2026-08", paths, "run1")
        self.assertFalse(any(p.exists() for p in paths))
        with tarfile.open(target, "r:gz") as archive:
            self.assertEqual(sorted(archive.getnames()), sorted(p.name for p in paths))

    def test_dry_run_changes_nothing(self):
        paths = [self.make("nvr-01", d) for d in range(100, 140)]
        with patch.object(archive_reports, "REPORTS_DIR", self.dir):
            self.assertEqual(archive_reports.main(["--older-than-days", "60", "--keep-per-nvr", "30"]), 0)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertFalse((self.dir / "archive").exists())


if __name__ == "__main__":
    unittest.main()
