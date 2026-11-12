"""Code review, 12 Nov 2026: deploy stops on failing tests, revoked Telegram
users stop getting alerts, slow dashboard clients are cut off and capped,
/uptime cannot stall the bot, CSV empty cells stay empty, and the event
status snapshot is a real copy."""
import base64
import shutil
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from nvr_track.bot import telegram_bot as tb
from nvr_track.core.paths import PROJECT_ROOT
from nvr_track.core.spreadsheet import csv_safe
from nvr_track.monitoring import uptime
from nvr_track.reports import custom_reports
from nvr_track.services import event_listener
from nvr_track.web import dashboard
from test_telegram_bot import TOKEN, Fixture, channel

SCRIPT = PROJECT_ROOT / "scripts" / "deploy_check.sh"


@unittest.skipUnless(SCRIPT.exists() and shutil.which("bash"), "needs bash and the script")
class DeployGateTests(unittest.TestCase):
    """Section 3 must stop the deploy before section 4 rebuilds and restarts anything."""

    def run_section_three(self, docker: str) -> subprocess.CompletedProcess:
        text = SCRIPT.read_text(encoding="utf-8")
        section = text.split('section "3.', 1)[1].split('section "4.', 1)[0].split("\n", 1)[1]
        stubs = ("set -u\nPASS=0; WARN=0; FAIL=0\npass() { echo \"PASS $*\"; }\n"
                 "fail() { echo \"FAIL $*\"; }\n" + docker + "\n")
        return subprocess.run(["bash", "-c", stubs + section + "\necho REACHED_SECTION_4\n"],
                              capture_output=True, text=True, timeout=30)

    def test_failing_tests_stop_the_deploy(self):
        result = self.run_section_three('docker() { [ "$1" = build ] && return 0; echo "1 failed, 491 passed"; }')
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL Test suite did not pass", result.stdout)
        self.assertNotIn("REACHED_SECTION_4", result.stdout)

    def test_a_broken_test_image_stops_the_deploy(self):
        result = self.run_section_three('docker() { return 1; }')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("REACHED_SECTION_4", result.stdout)

    def test_passing_tests_continue(self):
        result = self.run_section_three('docker() { [ "$1" = build ] && return 0; echo "492 passed in 9s"; }')
        self.assertEqual(result.returncode, 0)
        self.assertIn("PASS Test suite passed", result.stdout)
        self.assertIn("REACHED_SECTION_4", result.stdout)


class RevokedSubscriberTests(Fixture):
    def reload(self, **env):
        base = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ADMIN_USER_IDS": "111",
                "TELEGRAM_ALLOWED_CHAT_IDS": "-100500", "TELEGRAM_ALERT_CHAT_IDS": "-100900",
                "TELEGRAM_STATE_DIR": str(self.root / "tg")}
        self.bot = tb.Bot(tb.load_settings({**base, **env}), self.api)

    def test_removed_or_blocked_users_stop_receiving_alerts(self):
        self.report(channels=[channel(1)])
        self.message(222, "/alerts on")
        self.assertIn(222, self.bot.recipients())
        self.reload()                                               # 222 taken off the allowlist.
        self.assertNotIn(222, self.bot.recipients())
        self.reload(TELEGRAM_ALLOWED_USER_IDS="222")                # Allowed again: alerts resume.
        self.assertIn(222, self.bot.recipients())
        self.reload(TELEGRAM_ALLOWED_USER_IDS="222", TELEGRAM_BLOCKED_USER_IDS="222")
        self.assertNotIn(222, self.bot.recipients())

    def test_left_the_staff_group_stops_alerts(self):
        self.reload(TELEGRAM_ACCESS="group")
        member = {"status": "member"}
        test = self

        class API:
            def call(self, method, http_timeout=15, **params):
                test.calls.append((method, params))
                return dict(member) if method == "getChatMember" else {"message_id": 7}
        self.bot.api = API()
        self.report(channels=[channel(1)])
        self.message(5001, "/alerts on")
        self.assertIn(5001, self.bot.recipients())
        member["status"] = "left"
        self.bot.member_cache.clear()
        self.assertNotIn(5001, self.bot.recipients())

    def test_group_subscriptions_and_alert_chats_are_kept(self):
        self.report(channels=[channel(1)])
        self.message(111, "/alerts on", chat=-100500, chat_type="supergroup")
        self.assertTrue({-100500, -100900} <= self.bot.recipients())


class UptimeCommandTests(Fixture):
    def setUp(self):
        super().setUp()
        self.report(channels=[channel(1)])
        patcher = patch.object(tb.uptime_calc, "compute", return_value={"cameras": []})
        self.compute = patcher.start()
        self.addCleanup(patcher.stop)

    def test_long_ranges_are_for_admins(self):
        refused = self.message(222, "/uptime 120")
        self.assertIn("for administrators", refused[0]["text"])
        self.compute.assert_not_called()
        self.message(222, "/uptime 31")
        self.message(111, "/uptime 120")
        self.assertEqual(self.compute.call_count, 2)

    def test_repeated_requests_reuse_the_result(self):
        for _ in range(3):
            self.message(111, "/uptime 366")
        self.message(111, "/uptime 30")
        self.message(111, "/uptime 366")
        self.assertEqual(self.compute.call_count, 2)                # Was: one full read per request.
        start = self.compute.call_args_list[0].args[1]
        self.assertEqual((start.minute, start.second, start.microsecond), (0, 0, 0))


class CsvTests(unittest.TestCase):
    def test_csv_safe(self):
        self.assertEqual(csv_safe(""), "")                          # Was "'".
        self.assertEqual(csv_safe("Cam 1"), "Cam 1")
        self.assertEqual(csv_safe("-12.5"), "-12.5")
        for risky in ("=1+1", "+1", "-x", "@SUM(A1)", "\tcmd", "\rcmd"):
            self.assertEqual(csv_safe(risky), "'" + risky)

    def test_report_builder_csv_keeps_empty_cells_empty(self):
        section = {"columns": [{"label": "Camera", "kind": "text"}, {"label": "Days", "kind": "number"}],
                   "rows": [[None, 5], ["=HYPERLINK(1)", None], ["\tx", -3]]}
        lines = custom_reports.to_csv(section).decode("utf-8-sig").splitlines()
        self.assertEqual(lines[1:], [",5", "'=HYPERLINK(1),", "'\tx,-3"])

    def test_uptime_csv_keeps_empty_cells_empty(self):
        result = {"cameras": [{"nvr": "nvr-01", "nvr_name": "", "channel": "1", "name": "", "samples": 1,
                               "online_pct": None, "recording_pct": None, "checked": 0, "flaps": 0}]}
        self.assertEqual(uptime.to_csv(result).splitlines()[1], "nvr-01,,1,,1,,,0,0")


class EventStatusSnapshotTests(unittest.TestCase):
    def test_snapshot_does_not_share_nested_dicts(self):
        class NVR:
            id = "nvr-01"
        lock = threading.Lock()
        listener = event_listener.Listener(NVR(), {}, None, lock)
        listener.handle({"type": "unknownType", "channel": "1"})
        snapshot = event_listener.status_snapshot([listener], lock)
        listener.handle({"type": "anotherType", "channel": "1"})
        self.assertEqual(snapshot["nvr-01"]["ignored_types"], {"unknownType": 1})
        self.assertEqual(listener.state["ignored_types"], {"unknownType": 1, "anotherType": 1})


class SlowClientTests(unittest.TestCase):
    USER, PASSWORD = "demo", "test-password-long"

    def setUp(self):
        for target, value in (("DASHBOARD_USERNAME", self.USER), ("DASHBOARD_PASSWORD", self.PASSWORD),
                              ("LOGIN_GUARD", dashboard.LoginGuard()), ("REQUEST_DEADLINE_SECONDS", 1)):
            patcher = patch.object(dashboard, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def serve(self, **kwargs) -> int:
        server = dashboard.DashboardServer(("127.0.0.1", 0), dashboard.DashboardHandler, **kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def get(self, port: int) -> bytes:
        token = base64.b64encode(f"{self.USER}:{self.PASSWORD}".encode())
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            conn.sendall(b"GET /api/dashboard HTTP/1.0\r\nAuthorization: Basic " + token + b"\r\n\r\n")
            return conn.recv(20)

    def test_a_dripping_client_is_cut_off_at_the_deadline(self):
        port = self.serve()
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(conn.close)
        conn.sendall(b"GET / HTTP/1.1\r\n")
        started = time.monotonic()
        closed = False
        while time.monotonic() - started < 5:
            time.sleep(0.2)                                         # Always well under the read timeout.
            try:
                conn.sendall(b"X-Slow: y\r\n")
            except OSError:
                closed = True
                break
            conn.setblocking(False)
            try:
                closed = conn.recv(100) == b""
            except BlockingIOError:
                pass
            except OSError:
                closed = True
            conn.setblocking(True)
            if closed:
                break
        self.assertTrue(closed)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn(b" 200 ", self.get(port))

    def test_connections_past_the_cap_are_closed_at_once(self):
        port = self.serve(max_connections=2)
        held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
        for conn in held:
            self.addCleanup(conn.close)
            conn.sendall(b"GET / HTTP/1.1\r\n")                     # Unfinished requests hold both slots.
        time.sleep(0.3)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as extra:
            self.assertEqual(extra.recv(1), b"")
        for conn in held:
            conn.close()
        deadline = time.monotonic() + 3
        answer = b""
        while time.monotonic() < deadline and b" 200 " not in answer:
            time.sleep(0.1)
            answer = self.get(port)
        self.assertIn(b" 200 ", answer)


if __name__ == "__main__":
    unittest.main()
