"""Monthly PDF report: built from saved reports, the current state, the
period's real-time events and open known issues; saved and emailed by the
uptime report, served by the dashboard, sent by the Telegram bot. No NVR."""
import base64
import io
import json
import threading
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_addons import TempDirs
from test_telegram_bot import Fixture, channel

from app import dashboard, monthly_pdf, notes, uptime
from scripts import scan_worker, uptime_report

ICT = timezone(timedelta(hours=7))
START, END = datetime(2026, 10, 1, tzinfo=ICT), datetime(2026, 11, 1, tzinfo=ICT)


def text_of(pdf: bytes) -> str:
    """Text strings in an uncompressed reportlab PDF (built with compress=False)."""
    assert pdf.startswith(b"%PDF")
    return pdf.decode("latin-1")


def write_report(root, nvr_id, moment, statuses, restart=False):
    channels = [{"channel_id": str(i + 1), "name": f"Cam {i + 1}", "reported_status": live,
                 "recording_result": {"status": rec}} for i, (live, rec) in enumerate(statuses)]
    report = {"nvr": {"id": nvr_id, "name": "Kiln"}, "device_info": {"status": "DEVICE_INFO_RETRIEVED"},
              "check_completed_at": moment.isoformat(), "restart": {"detected": restart},
              "channel_inventory": {"channels": channels}, "overall": {"status": "WARNING"}}
    (root / f"{nvr_id}_{moment.strftime('%Y%m%dT%H%M%S%f%z')}.json").write_text(json.dumps(report))


class BuildTests(unittest.TestCase):
    def result(self):
        return {"start": START.isoformat(), "end": END.isoformat(),
                "fleet": {"nvrs": 1, "cameras": 2, "online_pct": 75.0, "recording_pct": 50.0},
                "nvrs": [{"nvr": "nvr-01", "name": "Kiln", "reports": 4, "restarts": 1, "reachable_pct": 100.0,
                          "cameras": 2, "online_pct": 75.0, "recording_pct": 50.0}],
                "cameras": [{"nvr": "nvr-01", "nvr_name": "Kiln", "channel": "1", "name": "Gate (east)",
                             "samples": 4, "live_known": 4, "online": 2, "checked": 4, "recording": 0, "flaps": 5,
                             "online_pct": 50.0, "recording_pct": 0.0},
                            {"nvr": "nvr-01", "nvr_name": "Kiln", "channel": "2", "name": "Yard ក <b>",
                             "samples": 4, "live_known": 4, "online": 4, "checked": 4, "recording": 4, "flaps": 0,
                             "online_pct": 100.0, "recording_pct": 100.0}]}

    def test_sections_and_honest_wording(self):
        current = {"nvrs": [{"id": "nvr-01", "name": "Kiln", "overall_status": "CRITICAL",
                             "critical_reasons": ["Storage needs urgent review: NVR reports abnormal HDD states: 3 (error)."],
                             "warnings": ["1 camera keeps going offline and online (4+ changes in 24 h): ch1."],
                             "kept_days": {"cameras": 1, "typical": 13, "fewest": 13, "window": 30},
                             "disks": [{"id": "3", "reported_status": "error", "smart": None},
                                       {"id": "5", "reported_status": "notexist", "smart": None},
                                       {"id": "8", "reported_status": "ok",
                                        "smart": {"status": "WARNING", "reallocated": 24, "pending": 8}}],
                             "channels": [{"channel_id": "1", "reported_status": "OFFLINE",
                                           "condition": {"code": "NO_FOOTAGE_30"}},
                                          {"channel_id": "2", "reported_status": "ONLINE", "condition": {"code": "OK"}}]}]}
        events = [{"nvr": "nvr-01", "type": "diskerror", "label": "Disk error"}] * 2
        known = [{"nvr": "nvr-01", "channel": "1", "reason": "Cable cut", "until": None,
                  "created_at": "2026-10-20T03:00:00+00:00", "by": "Heng"}]
        text = text_of(monthly_pdf.build(self.result(), period="2026-10", period_label="October 2026 (01 Oct - 31 Oct 2026)",
                                         current=current, events=events, known=known, compress=False))
        for expected in ("Monthly report", "October 2026", "Needs attention now", "Storage needs urgent review",
                         "keeps going offline and online", "Cameras with the lowest recording %", "Gate \\(east\\)",
                         "known issue", "13 days", "CRITICAL", "Disks to replace or watch", "24 reallocated, 8 pending",
                         "Disk error", "Known issues open", "Cable cut", "20 Oct 2026",
                         "not proof that it never", "or disabled checks are left out"):
            self.assertIn(expected, text, expected)
        self.assertNotIn("notexist", text)                                     # An empty bay is not a problem.
        self.assertNotIn("cameras without footage for 30+ days", text)        # ch1 is a known issue.
        self.assertIn("Yard ? &lt;b&gt;", monthly_pdf.clean("Yard ក <b>"))  # Non-Latin text cannot break it.

    def test_without_current_state_events_or_issues(self):
        text = text_of(monthly_pdf.build(self.result(), period="2026-10", period_label="October 2026",
                                         compress=False))
        self.assertIn("Current state not available.", text)
        self.assertIn("Real-time events are not available here.", text)
        empty = {**self.result(), "nvrs": [], "cameras": [], "fleet": {"nvrs": 0, "cameras": 0}}
        self.assertTrue(monthly_pdf.build(empty, period="2026-10", period_label="x").startswith(b"%PDF"))

    def test_period_labels(self):
        now = datetime(2026, 11, 6, 13, 0, tzinfo=ICT)
        self.assertEqual(monthly_pdf.period_label(START, END, "2026-10", now), "October 2026 (01 Oct – 31 Oct 2026)")
        self.assertEqual(monthly_pdf.period_label(END, datetime(2026, 12, 1, tzinfo=ICT), "2026-11", now),
                         "November 2026, month to date (01 Nov – 06 Nov 2026)")
        self.assertEqual(monthly_pdf.period_label(now - timedelta(days=7), now, "last 7 days", now),
                         "Last 7 days (30 Oct – 06 Nov 2026)")


class UptimeReportTests(TempDirs):
    def test_restarts_counted(self):
        write_report(self.root / "reports", "nvr-01", START + timedelta(hours=1), [("ONLINE", "RECORDING")])
        write_report(self.root / "reports", "nvr-01", START + timedelta(hours=3), [("ONLINE", "RECORDING")], restart=True)
        result = uptime.compute(self.root / "reports", START, END)
        self.assertEqual(result["nvrs"][0]["restarts"], 1)

    def test_pdf_is_saved_and_emailed_with_the_csv(self):
        for hour, live in ((1, "ONLINE"), (3, "OFFLINE")):
            write_report(self.root / "reports", "nvr-01", START + timedelta(hours=hour),
                         [(live, "RECORDING" if live == "ONLINE" else "NO_RECENT_RECORDING_EVIDENCE")])
        (self.root / "events" / "status.json").write_text(json.dumps({"schema_version": 1, "nvrs": {}}))
        with open(self.root / "events" / "events.jsonl", "w") as handle:
            for at in (START + timedelta(days=2), END + timedelta(days=1)):         # One inside, one after.
                handle.write(json.dumps({"nvr": "nvr-01", "type": "videoloss", "label": "Video loss",
                                         "severity": "critical", "channel": "1", "at": at.isoformat()}) + "\n")
        notes.add_known_issue("nvr-01", "1", "Cable cut", None, "Heng")
        sent = []
        with patch.object(uptime_report, "REPORTS_DIR", self.root / "reports"), \
                patch.object(uptime_report.mailer, "send", lambda *a: sent.append(a) or (True, "SENT to 1")), \
                patch.object(monthly_pdf, "build", wraps=monthly_pdf.build) as build, \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(uptime_report.main(["--month", "2026-10", "--email", "--pdf"]), 0)
        pdf = (self.root / "reports" / "uptime" / "NVR-Track-2026-10.pdf").read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn("Saved: reports/uptime/NVR-Track-2026-10.pdf", out.getvalue())
        self.assertEqual([name for name, _, _ in sent[0][3]], ["uptime-2026-10.csv", "NVR-Track-2026-10.pdf"])
        self.assertEqual(sent[0][3][1][2], "application/pdf")
        kwargs = build.call_args.kwargs
        self.assertEqual(len(kwargs["events"]), 1)                                      # Only October.
        self.assertEqual([k["reason"] for k in kwargs["known"]], ["Cable cut"])
        self.assertEqual(kwargs["period_label"][:12], "October 2026")

    def test_a_pdf_failure_still_sends_the_csv(self):
        write_report(self.root / "reports", "nvr-01", START + timedelta(hours=1), [("ONLINE", "RECORDING")])
        sent = []
        with patch.object(uptime_report, "REPORTS_DIR", self.root / "reports"), \
                patch.object(uptime_report.mailer, "send", lambda *a: sent.append(a) or (True, "SENT to 1")), \
                patch.object(monthly_pdf, "build", side_effect=ValueError("x")), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(uptime_report.main(["--month", "2026-10", "--email", "--pdf"]), 0)
        self.assertIn("PDF report failed: ValueError", out.getvalue())
        self.assertEqual([name for name, _, _ in sent[0][3]], ["uptime-2026-10.csv"])

    def test_scanner_asks_for_the_pdf(self):
        runs = []

        def fake_run(command, **kwargs):
            runs.append(command)

            class Result:
                returncode, stdout = 0, "Email report: SENT to 1\n"
            return Result
        with patch.object(scan_worker, "STATE_DIR", self.root / "scan"), \
                patch.object(scan_worker, "datetime") as clock, \
                patch.object(scan_worker.subprocess, "run", fake_run), patch("builtins.print"):
            clock.now.return_value = datetime(2026, 12, 1, 8, 5, tzinfo=scan_worker.TZ)
            scan_worker.Service().maybe_uptime_report()
        self.assertEqual(runs[0][2:], ["scripts.uptime_report", "--previous-month", "--email", "--pdf"])


class DashboardTests(TempDirs):
    def get(self, path):
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        token = base64.b64encode(b"demo:secret-password-123").decode()
        try:
            with urlopen(Request(f"http://127.0.0.1:{server.server_port}{path}",
                                 headers={"Authorization": "Basic " + token}), timeout=3) as response:
                return response.status, response.headers, response.read()
        except HTTPError as exc:
            return exc.code, exc.headers, b""
        finally:
            server.shutdown()
            server.server_close()

    def test_list_and_download(self):
        folder = self.root / "reports" / "uptime"
        folder.mkdir()
        (folder / "NVR-Track-2026-10.pdf").write_bytes(b"%PDF-1.4 test")
        (folder / "NVR-Track-2026-09.pdf").write_bytes(b"%PDF-1.4 old")
        (folder / "notes.pdf").write_bytes(b"%PDF other")
        self.assertEqual([r["period"] for r in dashboard.monthly_reports()], ["2026-10", "2026-09"])
        self.assertEqual([r["period"] for r in dashboard.read_dashboard_data()["monthly_reports"]],
                         ["2026-10", "2026-09"])
        with patch.object(dashboard, "DASHBOARD_USERNAME", "demo"), \
                patch.object(dashboard, "DASHBOARD_PASSWORD", "secret-password-123"):
            code, headers, body = self.get("/api/report.pdf?period=2026-10")
            self.assertEqual((code, headers["Content-Type"], body), (200, "application/pdf", b"%PDF-1.4 test"))
            self.assertIn('filename="NVR-Track-2026-10.pdf"', headers["Content-Disposition"])
            self.assertEqual(self.get("/api/report.pdf?period=2026-08")[0], 404)
            self.assertEqual(self.get("/api/report.pdf?period=../../.env")[0], 400)
        self.assertIn("/api/report.pdf?period=", dashboard.HTML)


class TelegramTests(Fixture):
    def setUp(self):
        super().setUp()
        self.uploads = []
        self.api.upload = lambda method, field, filename, data, mime, **p: self.uploads.append(
            (method, filename, data[:4], mime)) or {}

    def test_month_gets_the_pdf_last_days_do_not(self):
        self.report(channels=[channel(1)])
        last = datetime.now(ICT) - timedelta(minutes=5)
        month = last.strftime("%Y-%m")
        self.message(222, f"/uptime {month}")
        self.assertEqual([(u[1], u[2], u[3]) for u in self.uploads],
                         [(f"uptime-{month}.csv", b"\xef\xbb\xbfN", "text/csv"),
                          (f"NVR-Track-{month}.pdf", b"%PDF", "application/pdf")])
        self.uploads.clear()
        self.message(222, "/uptime 7")
        self.assertEqual([u[1] for u in self.uploads], ["uptime-last-7-days.csv"])

    def test_a_saved_pdf_is_used_for_a_finished_month(self):
        (self.root / "reports" / "uptime").mkdir()
        (self.root / "reports" / "uptime" / "NVR-Track-2026-10.pdf").write_bytes(b"%PDF saved")
        result = {"start": START.isoformat(), "end": END.isoformat(), "cameras": [1]}
        with patch.object(monthly_pdf, "pdf_for", side_effect=AssertionError("must not build")):
            self.bot.send_monthly_pdf(222, result, "2026-10")
        self.assertEqual(self.uploads, [("sendDocument", "NVR-Track-2026-10.pdf", b"%PDF", "application/pdf")])


if __name__ == "__main__":
    unittest.main()
