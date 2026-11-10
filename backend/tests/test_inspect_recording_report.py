import json
from pathlib import Path
import tempfile
import unittest

from nvr_track.cli.inspect_recording_report import channel_findings, latest_report, window_overlap


class ReportInspectorTests(unittest.TestCase):
    def test_summarizes_only_selected_channels(self):
        report = {"channel_inventory": {"channels": [
            {"channel_id": "12", "reported_status": "ONLINE", "recording_result": {
                "status": "RECORDING", "track_verified": True, "track_id": 1201,
                "most_recent_segment_start": "2026-10-26T03:00:00+00:00",
                "most_recent_segment_end": "2026-10-26T03:30:00+00:00",
                "requested_window_utc": {"start": "2026-10-26T02:30:00+00:00",
                                         "end": "2026-10-26T03:30:00+00:00"},
            }, "camera_ip": "192.0.2.12"},
            {"channel_id": "13", "reported_status": "OFFLINE"},
        ]}}
        found = channel_findings(report, {"12"})
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["recent_overlap"])
        self.assertNotIn("camera_ip", found[0])

    def test_absence_of_times_is_not_overlap(self):
        self.assertFalse(window_overlap({"status": "RECORDING"}))

    def test_report_selection_rejects_untrusted_nvr_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "nvr-11_1.json").write_text(json.dumps({"nvr": {"id": "nvr-11"}}))
            self.assertEqual(latest_report("nvr-11", root)["nvr"]["id"], "nvr-11")
            with self.assertRaises(ValueError):
                latest_report("../bad", root)
