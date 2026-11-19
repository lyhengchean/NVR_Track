"""Bug hunt 19 Nov 2026: one regression test per fix.

1. An NVR whose camera states were not read shows unknown counts, never
   "0 online / 0 offline" (dashboard, page, Telegram, email, report builder).
2. The monthly uptime report is retried after a failed run and caught up by the 7th.
3. Archiving never takes reports of this or last month; uptime says its real coverage.
4. The deploy check compares the dashboard with the inventory, not with 12.
5. SMART says the disk list was not read, not "no installed SATA disks".
6. Digest, known-issue reminder and monthly uptime post are retried when Telegram is down.
7. An event listener survives an unexpected error; a silent "connected" stream is down.
8. Quiet days of event-recording cameras are not "Gaps".
"""
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fake_nvr import FakeNVR
from test_addons import TempDirs
from test_telegram_bot import Fixture, calendar, channel

from nvr_track.bot import telegram_bot as tb
from nvr_track.bot import telegram_views as views
from nvr_track.cli import archive_reports
from nvr_track.cli import run_all_nvr_reports as fleet
from nvr_track.cli import run_nvr_report as runner
from nvr_track.core.paths import PROJECT_ROOT
from nvr_track.monitoring import uptime
from nvr_track.monitoring.camera_condition import classify
from nvr_track.reports import custom_reports, monthly_pdf
from nvr_track.services import event_listener, scan_worker
from nvr_track.web import dashboard
from frontend_source import DASHBOARD_UI

ICT = timezone(timedelta(hours=7))


def unreachable_report(nvr_id="nvr-03", expected=8, at=None):
    """What the reporter saves for an NVR that did not answer."""
    at = at or datetime.now(ICT) - timedelta(minutes=5)
    return {"schema_version": 2, "nvr": {"id": nvr_id, "name": "Dead", "ip": "10.9.9.3"},
            "check_completed_at": at.isoformat(),
            "device_info": {"status": "CONNECTION_ERROR"},
            "channel_inventory": {"status": "CONNECTION_ERROR", "expected_count": expected,
                                  "configured_count": None, "channels": []},
            "channel_status": {"status": "CONNECTION_ERROR", "online_count": 0, "offline_count": 0,
                               "unknown_count": 0},
            "storage": {"status": "UNKNOWN", "message": "Network request failed or timed out.", "disks": []},
            "per_camera_recording": {"enabled": True, "window_minutes": 60},
            "overall": {"status": "CRITICAL",
                        "critical_reasons": ["Device-information check failed: CONNECTION_ERROR."]}}


class UnknownCountTests(Fixture):
    def write(self, report):
        stamp = datetime.fromisoformat(report["check_completed_at"]).strftime("%Y%m%dT%H%M%S%f%z")
        path = self.root / "reports" / f"{report['nvr']['id']}_{stamp}.json"
        path.write_text(json.dumps(report), encoding="utf-8")

    def test_dashboard_and_telegram_say_unknown_not_zero(self):
        self.report(channels=[channel(1), channel(2, live="OFFLINE")])
        self.write(unreachable_report())
        data = dashboard.read_dashboard_data()
        dead = next(n for n in data["nvrs"] if n["id"] == "nvr-03")
        self.assertEqual((dead["online_channels"], dead["offline_channels"], dead["unknown_channels"]),
                         (None, None, 0))
        kiln = next(n for n in data["nvrs"] if n["id"] == "nvr-01")
        self.assertEqual((kiln["online_channels"], kiln["offline_channels"]), (1, 1))   # No status field: kept.
        s = data["summary"]
        self.assertEqual((s["cameras_online"], s["cameras_offline"], s["cameras_not_listed"]), (1, 1, 8))
        self.assertIn("known(nvr.online_channels)", DASHBOARD_UI)
        self.assertIn("online/offline unknown", views.nvr_detail(data, dead, False))
        self.assertNotIn("0 online", views.nvr_detail(data, dead, False))
        self.assertIn("Dead (nvr-03) — ?/8 online", views.nvr_list(data))
        self.assertIn("⛔ 8 no result (NVR unreachable)", views.fleet_status(data))

    def test_email_row_and_totals(self):
        nvr = SimpleNamespace(id="nvr-03", name="Dead", ip="10.9.9.3")
        row = fleet.report_row(nvr, unreachable_report(), {"schema_version": 1, "known_issues": [], "log": []})
        self.assertEqual((row["online"], row["offline"], row["cameras_listed"], row["cameras_unread"]),
                         (None, None, False, 8))
        ok = {"id": "nvr-01", "name": "Kiln", "ip": "10.9.9.1", "overall": "WARNING", "storage": "HEALTHY",
              "online": 3, "offline": 1, "recording_evidence": 3, "cameras_listed": True, "cameras_unread": 0}
        env = {"SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p",
               "SMTP_FROM": "a@example.com", "SMTP_TO": "b@example.com"}
        with patch.dict("os.environ", env), patch.object(fleet.smtplib, "SMTP") as smtp:
            fleet.send_email_report([ok, row])
        message = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        plain = message.get_body(("plain",)).get_content()
        html = message.get_body(("html",)).get_content()
        self.assertIn("Cameras online: 3; Cameras offline: 1; no result (camera list not read): 8", plain)
        self.assertIn("online=— | offline=— | cameras: camera list not read (8 expected)", plain)
        self.assertNotIn("online=0", plain)
        dead_row = html[html.index("nvr-03 - Dead"):].split("</tr>")[0]
        self.assertEqual(dead_row.count("<td>—</td>"), 8)                # Online … checks failed.
        self.assertIn("No result (camera list not read)</td><td><strong>8</strong>", html)

    def test_fleet_run_with_an_unreachable_nvr(self):
        """The whole fleet runner (console table, summary, email), as the scanner runs it."""
        inventory = self.root / "nvrs.json"
        inventory.write_text(json.dumps({"schema_version": 1, "nvrs": [
            {"id": "nvr-03", "ip": "10.9.9.3", "expected_channels": 8}]}), encoding="utf-8")
        saved = self.root / "reports" / "nvr-03_20261119T080000000000+0700.json"
        saved.write_text(json.dumps(unreachable_report()), encoding="utf-8")
        result = SimpleNamespace(returncode=1, stdout=f"JSON report saved: {saved}\n", stderr="")
        out = io.StringIO()
        with patch.object(fleet, "INVENTORY_PATH", inventory), patch.object(fleet, "REPORTS_DIR", self.root / "reports"), \
                patch.object(fleet, "run_report_with_lock_retry", return_value=result), \
                patch.object(fleet.notes_store, "load", return_value={"schema_version": 1, "known_issues": [], "log": []}), \
                patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            code = fleet.main(["--no-email"])
        self.assertEqual(code, 1)                                           # CRITICAL, not a runner error.
        line = next(line for line in out.getvalue().splitlines() if line.startswith("nvr-03"))
        self.assertEqual(line.split()[-2:], ["—", "—"])                    # Online, offline: unknown.

    def test_report_builder_rows(self):
        self.write(unreachable_report())
        today = datetime.now(ICT).date()
        spec, error = custom_reports.normalize({"sections": [{"dataset": "scans"}, {"dataset": "nvr_health"}],
                                                "date_from": (today - timedelta(days=1)).isoformat(),
                                                "date_to": today.isoformat()})
        self.assertIsNone(error)
        result = custom_reports.build(spec, self.root / "reports")
        scans, health = result["sections"]
        row = dict(zip([c["key"] for c in scans["columns"]], scans["rows"][0]))
        self.assertEqual((row["online"], row["offline"], row["recorded"], row["no_footage"]), (None,) * 4)
        row = dict(zip([c["key"] for c in health["columns"]], health["rows"][0]))
        self.assertIsNone(row["avg_offline"])                             # Not averaged in as 0.


class MonthlyUptimeTests(TempDirs):
    def run_on(self, moment, returncode):
        runs = []

        def fake_run(command, **kwargs):
            runs.append(command[2])
            return SimpleNamespace(returncode=returncode, stdout="Email report: FAILED: refused\n")
        with patch.object(scan_worker, "STATE_DIR", self.root / "scan"), \
                patch.object(scan_worker.subprocess, "run", fake_run), \
                patch.object(scan_worker, "datetime") as clock, patch("sys.stdout", new_callable=io.StringIO):
            clock.now.side_effect = lambda tz=None: moment
            scan_worker.Service().maybe_uptime_report()
        return runs

    def test_a_failed_run_is_retried_and_a_missed_1st_caught_up(self):
        first = datetime(2026, 12, 1, 8, 5, tzinfo=scan_worker.TZ)
        self.assertEqual(len(self.run_on(first, 2)), 1)                    # Email failed…
        self.assertEqual(len(self.run_on(first + timedelta(hours=2), 0)), 1)  # …tried again, now done.
        self.assertEqual(self.run_on(first + timedelta(hours=4), 0), [])   # Once a month.
        (self.root / "scan" / "uptime.json").unlink()
        self.assertEqual(len(self.run_on(datetime(2026, 12, 3, 6, 5, tzinfo=scan_worker.TZ), 0)), 1)
        (self.root / "scan" / "uptime.json").unlink()
        self.assertEqual(self.run_on(datetime(2026, 12, 9, 6, 5, tzinfo=scan_worker.TZ), 0), [])


class ArchiveAndCoverageTests(TempDirs):
    def write(self, nvr_id, moment, report=None):
        stamp = moment.strftime("%Y%m%dT%H%M%S%f%z")
        path = self.root / f"{nvr_id}_{stamp}.json"
        path.write_text(json.dumps(report or {"nvr": {"id": nvr_id}}), encoding="utf-8")
        return path

    def test_last_month_is_never_archived(self):
        now = datetime(2026, 12, 1, 6, 30, tzinfo=ICT)
        early_nov = self.write("nvr-01", datetime(2026, 11, 2, 8, 0, tzinfo=ICT))
        late_oct = self.write("nvr-01", datetime(2026, 10, 25, 8, 0, tzinfo=ICT))
        chosen = archive_reports.select(self.root, older_than_days=7, keep_per_nvr=0, now=now)
        self.assertEqual(chosen, {"2026-10": [late_oct]})
        self.assertTrue(early_nov.exists())

    def test_uptime_says_when_reports_start_late(self):
        start = datetime(2026, 11, 1, tzinfo=ICT)
        report = {"nvr": {"id": "nvr-01", "name": "Kiln"}, "device_info": {"status": "DEVICE_INFO_RETRIEVED"},
                  "channel_inventory": {"channels": [{"channel_id": "1", "reported_status": "ONLINE",
                                                      "recording_result": {"status": "RECORDING"}}]}}
        self.write("nvr-01", datetime(2026, 11, 20, 8, 0, tzinfo=ICT), report)
        result = uptime.compute(self.root, start, datetime(2026, 12, 1, tzinfo=ICT))
        note = uptime.coverage_note(result)
        self.assertIn("Saved reports cover only from 20 Nov 2026 08:00 (the period starts 01 Nov 2026)", note)
        self.assertIn(note, uptime.summary_lines(result, "t"))
        self.assertIn(note, views.uptime_text(result, "t"))
        pdf = monthly_pdf.build(result, period="2026-11", period_label="November 2026", compress=False)
        self.assertIn(b"Saved reports cover only from 20 Nov 2026", pdf)
        self.write("nvr-01", datetime(2026, 11, 1, 6, 0, tzinfo=ICT), report)
        self.assertIsNone(uptime.coverage_note(uptime.compute(self.root, start, datetime(2026, 12, 1, tzinfo=ICT))))


class DeployCheckTests(unittest.TestCase):
    def test_nvr_count_comes_from_the_inventory(self):
        text = (PROJECT_ROOT / "scripts" / "deploy_check.sh").read_text(encoding="utf-8")
        self.assertNotIn('"${TOTAL:-0}" = "12"', text)
        self.assertIn('[ "${TOTAL:-0}" = "$COUNT" ]', text)


class SmartDiskListTests(unittest.TestCase):
    def test_unread_disk_list_is_not_no_disks(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name in ("reports", "notes", "events", "snapshots", "config"):
            (root / name).mkdir()
        nvr = FakeNVR()
        self.addCleanup(nvr.close)
        nvr.forbidden = {"/ISAPI/ContentMgmt/Storage"}
        (root / "config" / "nvrs.local.json").write_text(json.dumps({"schema_version": 1, "nvrs": [
            {"id": "nvr-01", "ip": "127.0.0.1", "http_port": nvr.port, "expected_channels": 4}]}))
        with patch.object(runner, "ROOT", root), \
                patch.object(runner, "INVENTORY_PATH", root / "config" / "nvrs.local.json"), \
                patch.dict("os.environ", {"NVR_USERNAME": "admin", "NVR_PASSWORD": "not-a-real-password",
                                          "NVR_SMART_NVR_IDS": "nvr-01", "NVR_RECORDING_RETRY_DELAY_SECONDS": "0",
                                          "NOTES_DIR": str(root / "notes"), "EVENTS_DIR": str(root / "events"),
                                          "SNAPSHOT_DIR": str(root / "snapshots")}), \
                patch.object(sys, "argv", ["run_nvr_report", "--ip", "127.0.0.1", "--http-port", str(nvr.port)]), \
                patch("sys.stdout", new_callable=io.StringIO):
            runner.main()
        report = json.loads(next((root / "reports").glob("nvr-01_*.json")).read_text(encoding="utf-8"))
        smart = report["storage"]["smart"]
        self.assertEqual(smart["status"], "UNVERIFIED")
        self.assertEqual(smart["message"], "Not checked: the NVR's disk list could not be read.")
        self.assertFalse(any("SMARTTest" in path for _, path in nvr.requests))


class TelegramRetryTests(Fixture):
    def down(self, method, **params):
        raise tb.TelegramError("sendMessage: ConnectionError")

    def at(self, moment):
        clock = patch.object(tb, "datetime")
        mock = clock.start()
        self.addCleanup(clock.stop)
        mock.now.side_effect = lambda tz=None: moment if tz else moment.astimezone(timezone.utc)
        mock.fromisoformat = datetime.fromisoformat
        mock.side_effect = lambda *a, **k: datetime(*a, **k)
        return clock

    def sent(self, task):
        self.calls.clear()
        task()
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def test_digest_is_sent_once_telegram_is_back(self):
        self.report(channels=[channel(1)])
        self.at(datetime.now(views.LOCAL_TZ).replace(hour=8, minute=40))
        with patch.object(self.api, "call", side_effect=self.down):
            self.bot.check_digest()
        self.assertTrue(any("Daily summary" in t for t in self.sent(self.bot.check_digest)))
        self.assertEqual(self.sent(self.bot.check_digest), [])            # Once a day.

    def test_monthly_uptime_post_is_retried_and_caught_up(self):
        self.report(channels=[channel(1)])
        self.at(datetime(2026, 12, 3, 10, 0, tzinfo=views.LOCAL_TZ))     # Missed the 1st.
        result = {"start": "2026-11-01T00:00:00+07:00", "end": "2026-12-01T00:00:00+07:00",
                  "fleet": {"nvrs": 1, "cameras": 1, "online_pct": 100.0, "recording_pct": 100.0,
                            "first_report": "2026-11-01T06:00:00+07:00", "last_report": "2026-11-30T22:00:00+07:00"},
                  "nvrs": [], "cameras": [{"nvr": "nvr-01", "channel": "1", "name": "Cam 1", "flaps": 0,
                                           "recording_pct": 100.0, "online_pct": 100.0, "nvr_name": "",
                                           "samples": 1, "checked": 1}]}
        with patch.object(self.bot, "uptime_result", return_value=result), \
                patch.object(self.bot, "send_monthly_pdf"), patch.object(self.api, "upload", create=True):
            with patch.object(self.api, "call", side_effect=self.down):
                self.bot.check_monthly_uptime()
            self.assertTrue(any("Monthly uptime 2026-11" in t for t in self.sent(self.bot.check_monthly_uptime)))
            self.assertEqual(self.sent(self.bot.check_monthly_uptime), [])


class EventStreamTests(TempDirs):
    def test_listener_survives_an_unexpected_error(self):
        nvr = SimpleNamespace(id="nvr-01", ip="127.0.0.1", http_port=9)
        listener = event_listener.Listener(nvr, {}, None, __import__("threading").Lock())

        class Stop(Exception):
            pass
        with patch.dict("os.environ", {"NVR_USERNAME": "admin", "NVR_PASSWORD": "pw"}), \
                patch.object(event_listener.requests.Session, "get", side_effect=ValueError("odd reply")), \
                patch.object(event_listener.time, "sleep", side_effect=Stop), patch("builtins.print"):
            with self.assertRaises(Stop):                                  # Reached the retry wait.
                listener.run()
        self.assertEqual((listener.state["connected"], listener.state["last_error"]), (False, "ValueError"))

    def test_connected_but_silent_stream_is_down(self):
        now = datetime.now(timezone.utc)
        silent = (now - timedelta(minutes=10)).isoformat()
        status = {"nvr-01": {"connected": True, "since": (now - timedelta(hours=2)).isoformat(), "last_seen": silent},
                  "nvr-02": {"connected": True, "since": (now - timedelta(hours=2)).isoformat(),
                             "last_seen": (now - timedelta(seconds=20)).isoformat()}}
        with patch.object(dashboard.event_store, "read_status",
                          return_value={"updated_at": now.isoformat(), "nvrs": status}), \
                patch.object(dashboard.event_store, "recent_events", return_value=[]):
            nvrs = [{"id": "nvr-01"}, {"id": "nvr-02"}]
            dashboard._attach_events(nvrs)
        stuck, fine = nvrs[0]["event_stream"], nvrs[1]["event_stream"]
        self.assertEqual((stuck["connected"], stuck["last_error"], stuck["down_since"]), (False, "NO_DATA_TIMEOUT", silent))
        self.assertTrue(fine["connected"])


class EventCameraGapTests(unittest.TestCase):
    def test_quiet_days_of_an_event_camera_are_not_gaps(self):
        cam = channel(1)
        cam["retention_result"] = calendar(30)
        cam["retention_result"]["calendar"][10]["has_recording"] = False   # A day without motion.
        cam["retention_result"]["days_with_footage"] = 29
        self.assertEqual(classify(cam)["code"], "GAPS")                    # Continuous: a real gap.
        self.assertEqual(classify({**cam, "recording_expectation": "event"})["code"], "OK")


if __name__ == "__main__":
    unittest.main()
