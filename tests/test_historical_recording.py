"""Regression tests for bounded read-only historical recording monitoring."""
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.camera_recording import check_all_cameras
from app.historical_recording import (
    ABSENT, ERROR, FOUND, INCONCLUSIVE, NOT_CHECKED, intervals_for_history,
    search_history, valid_cache,
)
from app.recording import check_recording_evidence

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
TZ = timezone(timedelta(hours=7))


def no_match(**kwargs):
    return {"status": "NO_MATCHES_IN_WINDOW", "num_matches": 0, "matches": []}


def found(**kwargs):
    end = kwargs["window_end_utc"]
    return {"status": "RECORDING_EVIDENCE_FOUND", "matches": [{
        "track_id": kwargs["track_id"],
        "start_utc": (end - timedelta(minutes=20)).isoformat(),
        "end_utc": end.isoformat(),
    }]}


class HistoricalTests(unittest.TestCase):
    def test_intervals_cover_older_history_without_touching_recent_window(self):
        windows = intervals_for_history(NOW, 60, 7)
        self.assertEqual(len(windows), 7)
        self.assertEqual(windows[0][1], NOW - timedelta(hours=1))
        self.assertEqual(windows[-1][0], NOW - timedelta(days=7))
        for index, (start, end) in enumerate(windows):
            self.assertGreater(end, start)
            self.assertLessEqual(end-start, timedelta(days=1))
            if index:
                self.assertEqual(windows[index-1][0], end)

    def check(self, search, **kwargs):
        return search_history(
            ip="192.0.2.1", port=80, username="test", password="test",
            track_id=101, clock_snapshot=(NOW, TZ), recent_minutes=60,
            lookback_days=7, max_results=10,
            deadline=time.monotonic()+20, search=search, **kwargs,
        )

    def test_first_found_segment_stops_additional_queries(self):
        calls = []
        def search(**kwargs):
            calls.append(kwargs)
            return found(**kwargs)
        result = self.check(search)
        self.assertEqual(result["status"], FOUND)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result["intervals_examined"]), 1)
        self.assertFalse(result["search_complete"])

    def test_absence_requires_all_intervals(self):
        calls = []
        def search(**kwargs):
            calls.append(kwargs)
            return no_match(**kwargs)
        result = self.check(search)
        self.assertEqual(result["status"], ABSENT)
        self.assertTrue(result["search_complete"])
        self.assertEqual(len(calls), 7)
        self.assertEqual(result["num_matches"], 0)

    def test_error_stops_and_does_not_claim_absence(self):
        result = self.check(lambda **kwargs: {"status": "PERMISSION_DENIED"})
        self.assertEqual(result["status"], ERROR)
        self.assertEqual(len(result["intervals_examined"]), 1)

    def test_contradictory_success_is_inconclusive(self):
        def wrong_track(**kwargs):
            evidence = found(**kwargs)
            evidence["matches"][0]["track_id"] = 999
            return evidence
        result = self.check(wrong_track)
        self.assertEqual(result["status"], INCONCLUSIVE)

    def test_budget_prevents_first_request(self):
        result = search_history(
            ip="192.0.2.1", port=80, username="test", password="test",
            track_id=101, clock_snapshot=(NOW, TZ), recent_minutes=60,
            lookback_days=7, max_results=10,
            deadline=time.monotonic()-1,
            search=lambda **kwargs: self.fail("No request allowed after deadline"),
        )
        self.assertEqual(result["status"], INCONCLUSIVE)
        self.assertEqual(result["intervals_examined"], [])

    def test_cache_reuses_success_only_for_same_track_and_ttl(self):
        checked = datetime.now(timezone.utc)
        previous = {
            "status": FOUND, "track_id": 101,
            "checked_at": checked.isoformat(),
            "most_recent_segment_end": (NOW-timedelta(hours=2)).isoformat(),
        }
        args = {"now": checked, "ttl_hours": 12, "oldest": NOW-timedelta(days=7)}
        self.assertTrue(valid_cache(previous, 101, **args)["from_cache"])
        self.assertIsNone(valid_cache(previous, 201, **args))
        self.assertIsNone(valid_cache(previous, 101, now=checked+timedelta(hours=13),
                                      ttl_hours=12, oldest=NOW-timedelta(days=7)))
        previous["status"] = INCONCLUSIVE
        self.assertIsNone(valid_cache(previous, 101, **args))

    @patch('app.camera_recording.get_clock')
    @patch('app.camera_recording.get_tracks')
    def test_batch_queries_offline_and_limits_historical_cameras(self, tracks, clock):
        tracks.return_value = {"status": "VERIFIED", "tracks": {101: None, 201: None, 301: None}}
        clock.return_value = (NOW, TZ)
        channels = [
            {"channel_id": "1", "reported_status": "OFFLINE", "recording_expectation": "continuous"},
            {"channel_id": "2", "reported_status": "ONLINE", "recording_expectation": "continuous"},
            {"channel_id": "3", "reported_status": "ONLINE", "recording_expectation": "event"},
        ]
        history_calls = []
        def search(**kwargs):
            history_calls.append(kwargs)
            return no_match(**kwargs)
        summary = check_all_cameras(
            ip="192.0.2.1", port=80, username="test", password="test",
            channels=channels, enabled=True, search=no_match,
            historical_enabled=True, historical_max_cameras=1, historical_search=search,
        )
        self.assertEqual(summary["no_recent_evidence"], 3)
        self.assertEqual(summary["online_missing_expected"], 1)
        self.assertEqual(summary["historical"]["no_evidence"], 1)
        self.assertEqual(channels[0]["historical_recording_result"]["status"], ABSENT)
        self.assertEqual(channels[1]["historical_recording_result"]["status"], NOT_CHECKED)
        self.assertEqual(len(history_calls), 7)
        self.assertEqual({c["track_id"] for c in history_calls}, {101})

    @patch('app.camera_recording.get_clock')
    @patch('app.camera_recording.get_tracks')
    def test_daily_reporting_rotates_unchecked_historical_candidates(self, tracks, clock):
        """Expired 12-hour caches must not starve later channels at 08:00."""
        tracks.return_value = {
            "status": "VERIFIED", "tracks": {101: None, 201: None, 301: None, 401: None},
        }
        clock.return_value = (NOW, TZ)
        def run(previous=None):
            channels = [
                {"channel_id": str(i), "reported_status": "OFFLINE",
                 "recording_expectation": "unspecified"}
                for i in range(1, 5)
            ]
            calls = []
            def historical(**kwargs):
                calls.append(kwargs["track_id"])
                return no_match(**kwargs)
            check_all_cameras(
                ip="192.0.2.1", port=80, username="test", password="test",
                channels=channels, enabled=True, search=no_match,
                historical_enabled=True, historical_max_cameras=2,
                historical_ttl_hours=12, historical_search=historical,
                previous_history=previous,
            )
            return channels, calls

        first, calls = run()
        self.assertEqual(set(calls), {101, 201})
        previous = {c["channel_id"]: c["historical_recording_result"] for c in first}
        for i in ("1", "2"):
            # Simulate an 08:00 report from the previous day.
            previous[i]["checked_at"] = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
        second, calls = run(previous)
        self.assertEqual(set(calls), {301, 401})
        self.assertEqual(second[0]["historical_recording_result"]["status"], NOT_CHECKED)
        self.assertEqual(second[2]["historical_recording_result"]["status"], ABSENT)

        third_previous = {c["channel_id"]: c["historical_recording_result"] for c in second}
        third, calls = run(third_previous)
        self.assertEqual(set(calls), {101, 201})
        self.assertTrue(third[2]["historical_recording_result"]["from_cache"])

    @patch('app.camera_recording.get_clock')
    @patch('app.camera_recording.get_tracks')
    def test_valid_historical_cache_does_not_consume_camera_budget(self, tracks, clock):
        tracks.return_value = {"status": "VERIFIED", "tracks": {101: None, 201: None}}
        clock.return_value = (NOW, TZ)
        previous = {
            "1": {"status": ABSENT, "track_id": 101,
                  "checked_at": datetime.now(timezone.utc).isoformat(),
                  "search_complete": True},
        }
        calls = []
        channels = [{"channel_id": str(i), "reported_status": "OFFLINE"} for i in (1, 2)]
        check_all_cameras(
            ip="192.0.2.1", port=80, username="test", password="test",
            channels=channels, enabled=True, search=no_match,
            historical_enabled=True, historical_max_cameras=1,
            historical_search=lambda **kwargs: (calls.append(kwargs["track_id"]) or no_match(**kwargs)),
            previous_history=previous,
        )
        self.assertEqual(set(calls), {201})
        self.assertTrue(channels[0]["historical_recording_result"]["from_cache"])
        self.assertEqual(channels[1]["historical_recording_result"]["status"], ABSENT)

    def test_explicit_search_windows_reject_invalid_inputs_without_request(self):
        result = check_recording_evidence(
            ip="192.0.2.1", username="test", password="test",
            window_start_utc=NOW-timedelta(days=2), window_end_utc=NOW,
        )
        self.assertEqual(result["status"], "INVALID_CONFIGURATION")


if __name__ == "__main__":
    unittest.main()
