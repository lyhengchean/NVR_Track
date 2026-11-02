"""Regression tests: truncated (MORE) searches, JSON-safe results, report reading."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import dashboard
from app.camera_condition import classify
from app.camera_recording import check_all_cameras
from app.historical_recording import search_history
from app.recording import check_recording_evidence, refine_latest_segment
from scripts import run_all_nvr_reports as fleet

NS = "http://www.isapi.org/ver20/XMLSchema"
ICT = timezone(timedelta(hours=7))
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=ICT)


def fake_nvr(segments, max_results=10, log=None):
    """Search stub listing segments oldest first, truncated like the NVR."""
    def search(*, track_id, minutes=60, max_results=max_results, clock_snapshot=None,
               window_start_utc=None, window_end_utc=None, **_):
        now = clock_snapshot[0].astimezone(timezone.utc)
        high = window_end_utc or now
        low = window_start_utc or high - timedelta(minutes=minutes)
        if log is not None:
            log.append((low, high))
        hits = [(s, e) for s, e in segments if s <= high and e >= low]
        page = hits[:max_results]
        window = {"start": low.isoformat(), "end": high.isoformat()}
        if not page:
            return {"status": "NO_MATCHES_IN_WINDOW", "requested_window_utc": window,
                    "matches": [], "num_matches": 0, "more_available": False}
        return {"status": "RECORDING_EVIDENCE_FOUND", "requested_window_utc": window,
                "num_matches": len(page), "more_available": len(hits) > len(page),
                "matches": [{"track_id": track_id, "start_utc": s.isoformat(),
                             "end_utc": e.isoformat()} for s, e in page]}
    return search


def every(minutes, start, end, length=2):
    out, cursor = [], start
    while cursor < end:
        out.append((cursor, cursor + timedelta(minutes=length)))
        cursor += timedelta(minutes=minutes)
    return out


class SearchResultJsonTests(unittest.TestCase):
    def search(self, status_text):
        body = (f'<CMSearchResult xmlns="{NS}"><responseStatus>true</responseStatus>'
                f'<responseStatusStrg>{status_text}</responseStatusStrg><numOfMatches>1</numOfMatches>'
                '<matchList><searchMatchItem><trackID>101</trackID><timeSpan>'
                '<startTime>2026-11-02T08:20:00Z</startTime><endTime>2026-11-02T08:50:00Z</endTime>'
                '</timeSpan></searchMatchItem></matchList></CMSearchResult>').encode()
        with patch("app.recording.requests.Session") as session:
            session.return_value.__enter__.return_value.post.return_value = MagicMock(
                status_code=200, content=body)
            return check_recording_evidence("192.0.2.1", "u", "p", track_id=101,
                                            clock_snapshot=(NOW, ICT))

    def test_unrecognised_status_with_matches_can_be_saved(self):
        # Previously leaked parser datetimes and crashed the JSON report write.
        result = self.search("NO MORE MATCHES")
        self.assertEqual(result["status"], "UNKNOWN_SEARCH_STATUS")
        json.dumps(result)
        self.assertNotIn("_start", result["matches"][0])

    def test_more_flag_is_reported(self):
        self.assertTrue(self.search("MORE")["more_available"])
        self.assertFalse(self.search("OK")["more_available"])


class RefineLatestSegmentTests(unittest.TestCase):
    def test_busy_window_finds_newest_segment(self):
        now = NOW.astimezone(timezone.utc)
        segments = every(3, now - timedelta(minutes=60), now - timedelta(minutes=1))
        search = fake_nvr(segments)
        first = search(track_id=101, clock_snapshot=(NOW, ICT))
        self.assertTrue(first["more_available"])
        oldest_page_end = max(m["end_utc"] for m in first["matches"])
        refined = refine_latest_segment(
            first, track_id=101, max_searches=8,
            search_window=lambda lo, hi: search(track_id=101, clock_snapshot=(NOW, ICT),
                                                window_start_utc=lo, window_end_utc=hi))
        self.assertEqual(refined["end_utc"], segments[-1][1].isoformat())
        self.assertTrue(refined["complete"])
        self.assertGreater(refined["end_utc"], oldest_page_end)

    def test_newest_segment_in_earlier_half(self):
        now = NOW.astimezone(timezone.utc)
        segments = every(1, now - timedelta(minutes=60), now - timedelta(minutes=41))
        search = fake_nvr(segments)
        first = search(track_id=101, clock_snapshot=(NOW, ICT))
        refined = refine_latest_segment(
            first, track_id=101, max_searches=10,
            search_window=lambda lo, hi: search(track_id=101, clock_snapshot=(NOW, ICT),
                                                window_start_utc=lo, window_end_utc=hi))
        self.assertEqual(refined["end_utc"], segments[-1][1].isoformat())
        self.assertTrue(refined["complete"])

    def test_search_limit_keeps_lower_bound_marked_incomplete(self):
        now = NOW.astimezone(timezone.utc)
        segments = every(1, now - timedelta(minutes=60), now)
        log = []
        search = fake_nvr(segments, log=log)
        first = search(track_id=101, clock_snapshot=(NOW, ICT))
        log.clear()
        refined = refine_latest_segment(
            first, track_id=101, max_searches=1,
            search_window=lambda lo, hi: search(track_id=101, clock_snapshot=(NOW, ICT),
                                                window_start_utc=lo, window_end_utc=hi))
        self.assertEqual(len(log), 1)
        self.assertFalse(refined["complete"])

    def test_untruncated_result_needs_no_search(self):
        calls = []
        first = {"status": "RECORDING_EVIDENCE_FOUND", "more_available": False,
                 "requested_window_utc": {"start": "2026-11-02T01:00:00+00:00",
                                          "end": "2026-11-02T02:00:00+00:00"},
                 "matches": [{"track_id": 101, "start_utc": "2026-11-02T01:10:00+00:00",
                              "end_utc": "2026-11-02T01:59:00+00:00"}]}
        refined = refine_latest_segment(first, track_id=101,
                                        search_window=lambda *a: calls.append(a))
        self.assertEqual((refined["end_utc"], refined["complete"], calls),
                         ("2026-11-02T01:59:00+00:00", True, []))


class PerCameraRefinementTests(unittest.TestCase):
    @patch("app.camera_recording.get_clock")
    @patch("app.camera_recording.get_tracks")
    def test_status_unchanged_and_latest_end_corrected(self, tracks, clock):
        tracks.return_value = {"status": "VERIFIED", "tracks": {101: "1", 201: "2"}, "message": ""}
        clock.return_value = (NOW.astimezone(timezone.utc), ICT)
        now = NOW.astimezone(timezone.utc)
        busy = every(4, now - timedelta(minutes=60), now - timedelta(minutes=2))
        search = fake_nvr(busy)
        channels = [{"channel_id": "1", "reported_status": "ONLINE"},
                    {"channel_id": "2", "reported_status": "OFFLINE"}]
        check_all_cameras(ip="192.0.2.1", port=80, username="u", password="p",
                          channels=channels, enabled=True, search=search,
                          retry_delay_seconds=0)
        result = channels[0]["recording_result"]
        self.assertEqual(result["status"], "RECORDING")
        self.assertEqual(result["most_recent_segment_end"], busy[-1][1].isoformat())
        self.assertTrue(result["latest_segment_complete"])
        self.assertNotIn("_raw", result)
        json.dumps(channels)


class HistoricalRefinementTests(unittest.TestCase):
    def test_stopped_camera_reports_its_real_last_segment(self):
        now = NOW.astimezone(timezone.utc)
        stopped_at = now - timedelta(hours=5)
        # Continuous recording split into 30-minute files until it stopped.
        segments = every(30, now - timedelta(days=2), stopped_at, length=30)
        result = search_history(ip="192.0.2.1", port=80, username="u", password="p",
                                track_id=101, clock_snapshot=(NOW, ICT), recent_minutes=60,
                                lookback_days=7, max_results=10, deadline=float("inf"),
                                search=fake_nvr(segments))
        self.assertEqual(result["status"], "HISTORICAL_RECORDING_FOUND")
        self.assertEqual(result["most_recent_segment_end"], segments[-1][1].isoformat())
        self.assertTrue(result["latest_segment_complete"])


class DashboardReportReadingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write(self, name, completed, channel_name, nvr_id="nvr-01"):
        report = {"nvr": {"id": nvr_id}, "check_completed_at": completed,
                  "overall": {"status": "HEALTHY"},
                  "channel_inventory": {"channels": [{"channel_id": "1", "name": channel_name}]}}
        (self.root / name).write_text(json.dumps(report), encoding="utf-8")

    def test_only_newest_report_per_nvr_is_parsed(self):
        for day in range(1, 30):
            self.write(f"nvr-01_202610{day:02d}T080000123456+0700.json",
                       f"2026-10-{day:02d}T08:00:00+07:00", f"day {day}")
        opened = []
        real = dashboard._load_report
        with patch.object(dashboard, "REPORTS_DIR", self.root), \
                patch.object(dashboard, "_load_report", side_effect=lambda p: opened.append(p) or real(p)):
            data = dashboard.read_dashboard_data()
        self.assertEqual(len(opened), 1)
        self.assertEqual(data["nvrs"][0]["channels"][0]["name"], "day 29")

    def test_unreadable_newest_falls_back_to_previous(self):
        self.write("nvr-01_20261101T080000+0700.json", "2026-11-01T08:00:00+07:00", "previous")
        (self.root / "nvr-01_20261102T080000+0700.json").write_text("{", encoding="utf-8")
        with patch.object(dashboard, "REPORTS_DIR", self.root):
            data = dashboard.read_dashboard_data()
        self.assertEqual(data["report_errors"], 1)
        self.assertEqual(data["nvrs"][0]["channels"][0]["name"], "previous")

    def test_last_report_compares_time_not_text(self):
        self.write("nvr-01_20261102T090000+0700.json", "2026-11-02T09:00:00+07:00", "a")
        self.write("nvr-02_20261102T030000+0000.json", "2026-11-02T03:00:00+00:00", "b", "nvr-02")
        with patch.object(dashboard, "REPORTS_DIR", self.root):
            summary = dashboard.read_dashboard_data()["summary"]
        self.assertEqual(summary["last_successful_report"], "2026-11-02T03:00:00+00:00")

    def test_manifest_accepts_inventory_style_ids(self):
        manifest = {"schema_version": 1, "started_at": "2026-11-02T01:00:00+00:00",
                    "completed_at": "2026-11-02T01:30:00+00:00",
                    "devices": [{"id": "nvr-01", "result": "OK"},
                                {"id": "site_b-nvr", "result": "ERROR", "reason": "PROCESS_TIMEOUT"}]}
        (self.root / "fleet_latest_run.json").write_text(json.dumps(manifest), encoding="utf-8")
        with patch.object(dashboard, "REPORTS_DIR", self.root):
            failed, started, errors = dashboard._read_fleet_run_state()
        self.assertEqual((failed, errors), ({"site_b-nvr": "PROCESS_TIMEOUT"}, 0))

    def test_csp_allows_only_the_page_inline_code(self):
        self.assertIn("default-src 'none'", dashboard.CSP)
        self.assertIn("script-src 'sha256-", dashboard.CSP)
        self.assertNotIn("unsafe-inline", dashboard.CSP)


class ConditionAndRunnerTests(unittest.TestCase):
    def test_condition_text_uses_configured_window(self):
        channel = {"reported_status": "ONLINE",
                   "recording_result": {"status": "NO_RECENT_RECORDING_EVIDENCE", "window_minutes": 120}}
        self.assertIn("the last 2 hours", classify(channel)["detail"])

    def test_invalid_budget_does_not_fail_every_nvr(self):
        with patch.dict(os.environ, {"NVR_RECORDING_MAX_NVR_SECONDS": "5m"}):
            self.assertEqual(fleet.report_timeout_seconds(), 600)
        with patch.dict(os.environ, {"NVR_RECORDING_MAX_NVR_SECONDS": "900"}):
            self.assertEqual(fleet.report_timeout_seconds(), 1200)


if __name__ == "__main__":
    unittest.main()
