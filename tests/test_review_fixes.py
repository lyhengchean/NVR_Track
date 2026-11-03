"""Regressions for the November 2026 code review fixes. No NVR is contacted."""
import base64
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import dashboard
from app.camera_condition import classify
from app.camera_recording import check_all_cameras
from app.retention import STALE, VERIFIED
from scripts import run_nvr_report as runner
from scripts import scan_worker as worker
from scripts.run_all_nvr_reports import camera_conditions

ICT = timezone(timedelta(hours=7))
EVERY_2H = worker.parse_times("06:00,08:00,10:00,12:00,14:00,16:00,18:00,20:00,22:00", "08:00")
EMAIL_8 = worker.parse_times("08:00", "08:00")


def at(hour, minute=0, day=2):
    return datetime(2026, 11, day, hour, minute, tzinfo=ICT)


class NonAsciiLoginTests(unittest.TestCase):
    """compare_digest(str, str) raised TypeError for non-ASCII input, which
    dropped the connection instead of answering 401 (or logging in)."""

    def request(self, user_password: str) -> int:
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        token = base64.b64encode(user_password.encode("utf-8")).decode()
        try:
            with urlopen(Request(f"http://127.0.0.1:{server.server_port}/",
                                 headers={"Authorization": "Basic " + token}), timeout=3) as response:
                return response.status
        except HTTPError as exc:
            return exc.code
        finally:
            server.shutdown()
            server.server_close()

    def test_wrong_non_ascii_password_gets_401(self):
        with patch.object(dashboard, "DASHBOARD_USERNAME", "demo"), \
                patch.object(dashboard, "DASHBOARD_PASSWORD", "test-password-long"):
            self.assertEqual(self.request("demo:ពាក្យសម្ងាត់"), 401)

    def test_non_ascii_password_can_log_in(self):
        with patch.object(dashboard, "DASHBOARD_USERNAME", "demo"), \
                patch.object(dashboard, "DASHBOARD_PASSWORD", "ពាក្យសម្ងាត់-long-pass"):
            self.assertEqual(self.request("demo:ពាក្យសម្ងាត់-long-pass"), 200)
            self.assertEqual(self.request("demo:ពាក្យសម្ងាត់-long-pasX"), 401)


def camera(status, live="ONLINE", expectation="continuous", history="HISTORICAL_NOT_CHECKED",
           calendar=None, history_window=None):
    return {"channel_id": "5", "name": "Yard", "reported_status": live,
            "recording_expectation": expectation,
            "recording_result": {"status": status, "window_minutes": 60},
            "historical_recording_result": {"status": history,
                                            "requested_window_utc": history_window or {}},
            "retention_result": calendar or {"status": "RETENTION_NOT_CONFIGURED"}}


def calendar(days_with_footage):
    rows = [{"date": f"2026-10-{d:02d}", "has_recording": d > 30 - days_with_footage}
            for d in range(1, 31)]
    return {"status": "RETENTION_VERIFIED", "window_days": 30,
            "days_with_footage": days_with_footage, "calendar": rows}


class EventCameraConditionTests(unittest.TestCase):
    """README and reporting.py: only continuous cameras alert on a quiet window."""

    def test_online_event_camera_without_recent_segment_is_not_stopped(self):
        result = classify(camera("NO_RECENT_RECORDING_EVIDENCE", expectation="event",
                                 history="HISTORICAL_RECORDING_FOUND"))
        self.assertEqual((result["code"], result["severity"]), ("NO_RECENT_EVENTS", "info"))
        self.assertIn("no events in the last hour", result["detail"])

    def test_continuous_and_unspecified_cameras_still_stop(self):
        for expectation in ("continuous", "unspecified"):
            result = classify(camera("NO_RECENT_RECORDING_EVIDENCE", expectation=expectation,
                                     history="HISTORICAL_RECORDING_FOUND"))
            self.assertEqual(result["code"], "NOT_RECORDING", expectation)

    def test_offline_or_30_empty_days_stay_problems_for_event_cameras(self):
        self.assertEqual(classify(camera("NO_RECENT_RECORDING_EVIDENCE", live="OFFLINE",
                                         expectation="event"))["code"], "NOT_RECORDING")
        self.assertEqual(classify(camera("NO_RECENT_RECORDING_EVIDENCE", expectation="event",
                                         calendar=calendar(0)))["code"], "NO_FOOTAGE_30")

    def test_email_does_not_count_quiet_event_cameras_as_stopped(self):
        report = {"channel_inventory": {"channels": [
            camera("NO_RECENT_RECORDING_EVIDENCE", expectation="event", calendar=calendar(30)),
            camera("NO_RECENT_RECORDING_EVIDENCE", expectation="continuous", calendar=calendar(30)),
        ]}}
        self.assertEqual(camera_conditions(report)["stopped_recording"], 1)

    def test_history_text_follows_configured_lookback(self):
        now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
        window = {"start": (now - timedelta(days=3)).isoformat(),
                  "end": (now - timedelta(minutes=60)).isoformat()}
        result = classify(camera("NO_RECENT_RECORDING_EVIDENCE", history="HISTORICAL_RECORDING_FOUND",
                                 history_window=window))
        self.assertIn("older video found in the last 3 days", result["detail"])

    def test_dashboard_exposes_new_condition(self):
        self.assertIn("NO_RECENT_EVENTS", dashboard.CONDITIONS)
        self.assertIn('"NO_RECENT_EVENTS"', dashboard.HTML)
        self.assertIn(".k-NO_RECENT_EVENTS", dashboard.HTML)


class MissedScheduledScanTests(unittest.TestCase):
    """A slot that fell due during a long scan used to be skipped after 30 min."""

    def test_slot_due_during_a_running_scan_runs_when_it_ends(self):
        # Manual "Scan all" 07:45-08:40: the 08:00 scan (with email) still runs.
        self.assertIsNone(worker.due_slot(at(8, 40), EVERY_2H, at(6)))
        self.assertEqual(worker.due_slot(at(8, 40), EVERY_2H, at(6), busy_since=at(7, 45)), at(8))

    def test_restart_rule_is_unchanged(self):
        # Slot older than the scan that just ran is not "missed while busy".
        self.assertIsNone(worker.due_slot(at(9, 0), EVERY_2H, at(6), busy_since=at(8, 50)))
        self.assertEqual(worker.due_slot(at(8, 20), EVERY_2H, at(6)), at(8))
        self.assertIsNone(worker.due_slot(at(8, 40), EVERY_2H, at(8), busy_since=at(7, 45)))

    def test_email_follows_a_passed_over_email_slot_once(self):
        self.assertTrue(worker.email_due(at(8), at(6), EMAIL_8))
        self.assertFalse(worker.email_due(at(10), at(8), EMAIL_8))
        # 06:00 scan overran both 08:00 and 10:00: the 10:00 run sends the email.
        self.assertTrue(worker.email_due(at(10), at(6), EMAIL_8))
        self.assertFalse(worker.email_due(at(6, day=3), at(22), EMAIL_8))
        self.assertTrue(worker.email_due(at(8), None, EMAIL_8))
        self.assertFalse(worker.email_due(at(10), None, EMAIL_8))


class PreviousResultsSurviveFailedRunTests(unittest.TestCase):
    """A report from an unreachable run (no camera rows) used to hide every
    cached calendar and history result from the next run."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def save(self, stamp, channels):
        report = {"nvr": {"id": "nvr-01"}, "channel_inventory": {"channels": channels}}
        (self.dir / f"nvr-01_{stamp}+0700.json").write_text(json.dumps(report), encoding="utf-8")

    def test_newest_timed_result_per_camera_is_used(self):
        good = {"status": VERIFIED, "track_id": 101, "checked_at": "2026-11-02T01:00:00+00:00"}
        newer = {"status": VERIFIED, "track_id": 201, "checked_at": "2026-11-02T03:00:00+00:00"}
        self.save("20261102T080000000000", [{"channel_id": "1", "retention_result": good},
                                            {"channel_id": "2", "retention_result": good}])
        self.save("20261102T100000000000", [{"channel_id": "2", "retention_result": newer}])
        self.save("20261102T120000000000", [])  # NVR unreachable.
        self.save("20261102T140000000000", [   # Track list failed: blank results.
            {"channel_id": "1", "retention_result": {"status": "RETENTION_UNVERIFIED", "checked_at": None}}])
        previous = runner.read_previous_retention(self.dir, "nvr-01")
        self.assertEqual(previous, {"1": good, "2": newer})

    def test_reports_of_other_devices_are_ignored(self):
        (self.dir / "nvr-01_x_20261102T080000000000+0700.json").write_text(json.dumps(
            {"nvr": {"id": "nvr-01_x"}, "channel_inventory": {"channels": [
                {"channel_id": "1", "historical_recording_result": {"checked_at": "2026-11-02T01:00:00+00:00"}}]}}))
        self.assertEqual(runner.read_previous_history(self.dir, "nvr-01"), {})


class CalendarCutByBudgetTests(unittest.TestCase):
    """A calendar scan stopped by the time budget used to replace the earlier
    calendar with an empty pending one."""

    @patch("app.camera_recording.get_clock")
    @patch("app.camera_recording.get_tracks")
    def test_previous_calendar_is_kept_as_stale(self, tracks, clock):
        tracks.return_value = {"status": "VERIFIED", "tracks": {101: "1"}, "message": ""}
        now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
        clock.return_value = now, ICT
        old = {"status": VERIFIED, "track_id": 101, "as_of_date": "2026-11-01", "window_days": 30,
               "checked_at": "2026-11-01T01:00:00+00:00", "last_attempted_at": "2026-11-01T01:00:00+00:00",
               "calendar": [{"date": "2026-10-31", "has_recording": True}], "days_with_footage": 30,
               "today": {"date": "2026-11-01", "has_recording": True}}

        def cut_by_deadline(**params):
            return {"status": "RETENTION_PENDING", "track_id": 101, "checked_at": None,
                    "last_attempted_at": now.isoformat(), "calendar": []}
        channels = [{"channel_id": "1", "reported_status": "ONLINE"}]
        check_all_cameras(ip="192.0.2.10", port=80, username="x", password="y", channels=channels,
                          enabled=True, search=lambda **p: {"status": "RECORDING_EVIDENCE_FOUND", "matches": []},
                          retention_enabled=True, retention_max_cameras=1,
                          previous_retention={"1": old}, retention_scan=cut_by_deadline)
        result = channels[0]["retention_result"]
        self.assertEqual(result["status"], STALE)
        self.assertEqual(result["days_with_footage"], 30)
        self.assertEqual(result["calendar"], old["calendar"])


class DashboardReportDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patcher = patch.object(dashboard, "REPORTS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        state = patch.object(dashboard, "SCAN_STATE_DIR", self.root / "scan")
        state.start()
        self.addCleanup(state.stop)

    def write(self, nvr_id, completed):
        report = {"nvr": {"id": nvr_id}, "check_completed_at": completed.isoformat(),
                  "overall": {"status": "HEALTHY"}}
        stamp = completed.astimezone(ICT).strftime("%Y%m%dT%H%M%S%f%z")
        (self.root / f"{nvr_id}_{stamp}.json").write_text(json.dumps(report), encoding="utf-8")

    def test_any_inventory_id_is_shown_and_other_json_is_not_an_error(self):
        now = datetime.now(timezone.utc)
        self.write("site-a", now)
        self.write("nvr-02", now)
        (self.root / "fleet_latest_run.json").write_text("{}", encoding="utf-8")
        (self.root / "track-verification-20261102T080000000000+0700.json").write_text("{}", encoding="utf-8")
        data = dashboard.read_dashboard_data()
        self.assertEqual([n["id"] for n in data["nvrs"]], ["nvr-02", "site-a"])
        self.assertEqual(data["report_errors"], 1)  # Only the (invalid) fleet manifest itself.

    def scan_status(self, times):
        (self.root / "scan").mkdir(exist_ok=True)
        (self.root / "scan" / "status.json").write_text(json.dumps({
            "schema_version": 1, "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "scan_times": times, "email_times": ["08:00"]}), encoding="utf-8")

    def test_stale_follows_the_scan_schedule(self):
        now = datetime(2026, 11, 2, 14, 30, tzinfo=ICT)
        cutoff, slot = dashboard._stale_cutoff(now, ["06:00", "08:00", "10:00", "12:00", "14:00"])
        self.assertEqual(slot, datetime(2026, 11, 2, 12, 0, tzinfo=dashboard.LOCAL_TZ))
        self.assertEqual(cutoff, slot - timedelta(minutes=10))
        # Overnight: the 22:00 report stays current until the 06:00 scan has had time to run.
        _, night = dashboard._stale_cutoff(datetime(2026, 11, 3, 7, 30, tzinfo=ICT),
                                           ["06:00", "22:00"])
        self.assertEqual(night, datetime(2026, 11, 2, 22, 0, tzinfo=dashboard.LOCAL_TZ))
        # No schedule: the previous 26-hour rule.
        cutoff, slot = dashboard._stale_cutoff(now, [])
        self.assertEqual((cutoff, slot), (now - timedelta(hours=26), None))

    def test_dashboard_marks_reports_that_missed_scheduled_scans(self):
        now = datetime.now(timezone.utc)
        self.write("nvr-01", now - timedelta(hours=10))
        self.write("nvr-02", now - timedelta(minutes=5))
        self.scan_status([f"{h:02d}:00" for h in range(24)])  # Hourly.
        data = dashboard.read_dashboard_data()
        stale = {n["id"]: n["stale"] for n in data["nvrs"]}
        self.assertEqual(stale, {"nvr-01": True, "nvr-02": False})
        self.assertEqual(data["summary"]["stale_reports"], 1)
        self.assertIsNotNone(data["summary"]["stale_slot"])
        self.scan_status([])  # No schedule: 10 hours is still within 26 hours.
        self.assertEqual(dashboard.read_dashboard_data()["summary"]["stale_reports"], 0)


class RolloutAdviceTests(unittest.TestCase):
    def test_only_fully_verified_nvrs_are_suggested(self):
        from scripts.verify_recording_tracks import rollout_advice
        mapped = [{"status": "VERIFIED"}] * 3
        report = [
            {"nvr_id": "nvr-01", "track_list_status": "VERIFIED", "channels": mapped},
            {"nvr_id": "nvr-02", "track_list_status": "VERIFIED", "channels": mapped},
            {"nvr_id": "nvr-03", "track_list_status": "VERIFIED",
             "channels": mapped[:2] + [{"status": "TRACK_NOT_FOUND"}]},
            {"nvr_id": "nvr-04", "track_list_status": "PERMISSION_DENIED", "channels": []},
            {"nvr_id": "nvr-05", "track_list_status": "VERIFIED", "inventory_status": "TIMEOUT", "channels": []},
        ]
        text = "\n".join(rollout_advice(report, "nvr-01"))
        self.assertIn("Fully verified: nvr-01, nvr-02", text)
        self.assertIn("nvr-03 (2/3 cameras mapped)", text)
        self.assertIn("nvr-04 (track list PERMISSION_DENIED)", text)
        self.assertIn("nvr-05 (camera list TIMEOUT)", text)
        self.assertIn("NVR_PER_CAMERA_RECORDING_IDS=nvr-01,nvr-02\n", text + "\n")
        self.assertIn("already enabled", "\n".join(rollout_advice(report, "nvr-02, NVR-01")))


if __name__ == "__main__":
    unittest.main()
