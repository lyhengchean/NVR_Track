"""A rejected search reply says which rule failed, in the report and in the
read-only explain tool (nvr-01 ch47 "Elevator Exit": "could not be validated")."""
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fake_nvr import FakeNVR

from nvr_track.nvr.recording import check_recording_evidence, parse_search_response
from nvr_track.cli import explain_recording_search as explain

FMT = "%Y-%m-%dT%H:%M:%SZ"


def reply_with_zero_length_segment(track: int) -> str:
    """Two segments; the second opens and closes in the same second."""
    now = datetime.now().replace(microsecond=0)
    seg = lambda a, b: (f"<searchMatchItem><trackID>{track}</trackID><timeSpan><startTime>{a.strftime(FMT)}"
                        f"</startTime><endTime>{b.strftime(FMT)}</endTime></timeSpan></searchMatchItem>")
    return ("<CMSearchResult><responseStatus>true</responseStatus><responseStatusStrg>OK</responseStatusStrg>"
            "<numOfMatches>2</numOfMatches><matchList>"
            + seg(now - timedelta(minutes=40), now - timedelta(minutes=10))
            + seg(now - timedelta(minutes=5), now - timedelta(minutes=5))
            + "</matchList></CMSearchResult>")


def paged_reply(track: int, returned: int, reported: int, status: str = "MORE") -> str:
    """nvr-01 ch47 at 15:17: MORE, numOfMatches 12, 10 short motion segments."""
    now = datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None, microsecond=0)  # NVR wall clock.
    items = "".join(
        f"<searchMatchItem><trackID>{track}</trackID><timeSpan>"
        f"<startTime>{(now - timedelta(minutes=58 - 5 * i)).strftime(FMT)}</startTime>"
        f"<endTime>{(now - timedelta(minutes=56 - 5 * i)).strftime(FMT)}</endTime></timeSpan></searchMatchItem>"
        for i in range(returned))
    return (f"<CMSearchResult><responseStatus>true</responseStatus><responseStatusStrg>{status}</responseStatusStrg>"
            f"<numOfMatches>{reported}</numOfMatches><matchList>{items}</matchList></CMSearchResult>")


class PagedReplyTests(unittest.TestCase):
    """A full page with MORE may report the count of all matches."""

    def test_parser_accepts_only_a_full_truncated_page(self):
        accepted = parse_search_response(paged_reply(4701, 10, 12).encode(), page_size=10)
        self.assertEqual((accepted["num_matches"], accepted["total_matches"], accepted["search_status"]),
                         (10, 12, "MORE"))
        for xml, size in ((paged_reply(4701, 9, 12), 10),          # Not a full page.
                          (paged_reply(4701, 10, 12, "OK"), 10),   # Not MORE.
                          (paged_reply(4701, 10, 8), 10),          # Fewer reported than returned.
                          (paged_reply(4701, 10, 12), None)):      # Page size unknown.
            with self.assertRaises(ValueError):
                parse_search_response(xml.encode(), page_size=size)

    def test_motion_camera_with_more_segments_than_one_page_counts_as_recording(self):
        nvr = FakeNVR()
        self.addCleanup(nvr.close)
        nvr.search_replies["2"] = paged_reply(201, 10, 12)
        result = check_recording_evidence("127.0.0.1", "admin", "x", port=nvr.port, track_id=201, max_results=10)
        self.assertEqual(result["status"], "RECORDING_EVIDENCE_FOUND")
        self.assertTrue(result["more_available"])               # Newest segment is then searched for.
        self.assertEqual((result["num_matches"], result["total_matches"]), (10, 12))

    def test_per_camera_check_reports_recording_and_stays_bounded(self):
        from nvr_track.nvr.camera_recording import check_all_cameras
        nvr = FakeNVR()
        self.addCleanup(nvr.close)
        nvr.search_replies["2"] = paged_reply(201, 10, 12)
        channels = [{"channel_id": "2", "name": "Elevator Exit", "reported_status": "ONLINE"}]
        check_all_cameras(ip="127.0.0.1", port=nvr.port, username="admin", password="x", channels=channels,
                          enabled=True, minutes=60, max_results=10, retry_delay_seconds=0)
        self.assertEqual(channels[0]["recording_result"]["status"], "RECORDING")
        searches = sum(p == "/ISAPI/ContentMgmt/search" for m, p in nvr.requests if m == "POST")
        self.assertLessEqual(searches, 1 + 8)                    # First page + bounded newest-segment search.


class ExplainSearchTests(unittest.TestCase):
    def setUp(self):
        self.nvr = FakeNVR()
        self.addCleanup(self.nvr.close)
        self.nvr.search_replies["2"] = reply_with_zero_length_segment(201)

    def test_report_message_names_the_failed_rule(self):
        result = check_recording_evidence("127.0.0.1", "admin", "x", port=self.nvr.port, track_id=201)
        self.assertEqual(result["status"], "INCONSISTENT_OR_UNEXPECTED_RESPONSE")
        self.assertEqual(result["message"], "Recording-search response could not be validated: "
                                            "A search result has an invalid track or time range.")

    def test_explain_tool_marks_the_bad_segment(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "config").mkdir()
            (Path(temp) / "config" / "nvrs.local.json").write_text(json.dumps({"schema_version": 1, "nvrs": [
                {"id": "nvr-01", "ip": "127.0.0.1", "http_port": self.nvr.port, "expected_channels": 4}]}))
            argv = ["explain", "--nvr-id", "nvr-01", "--channel", "2"]
            with patch.object(explain, "ROOT", Path(temp)), patch.object(sys, "argv", argv), \
                    patch.dict("os.environ", {"NVR_USERNAME": "admin", "NVR_PASSWORD": "secret-pass"}), \
                    patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(explain.main(), 0)
        text = out.getvalue()
        self.assertIn("Segments in the reply: 2", text)
        self.assertIn("<-- end is not after start (+0 s)", text)
        self.assertIn("Result: REJECTED by NVR_Track: A search result has an invalid track or time range.", text)
        self.assertNotIn("secret-pass", text)
        posts = [p for m, p in self.nvr.requests if m == "POST"]
        self.assertEqual(posts, ["/ISAPI/ContentMgmt/search"])            # Read-only: one search.

    def test_explain_tool_on_a_normal_camera(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "config").mkdir()
            (Path(temp) / "config" / "nvrs.local.json").write_text(json.dumps({"schema_version": 1, "nvrs": [
                {"id": "nvr-01", "ip": "127.0.0.1", "http_port": self.nvr.port, "expected_channels": 4}]}))
            with patch.object(explain, "ROOT", Path(temp)), \
                    patch.object(sys, "argv", ["explain", "--nvr-id", "nvr-01", "--channel", "1"]), \
                    patch.dict("os.environ", {"NVR_USERNAME": "admin", "NVR_PASSWORD": "x"}), \
                    patch("sys.stdout", new_callable=io.StringIO) as out:
                explain.main()
        self.assertIn("Result: accepted; 1 segment(s), status OK", out.getvalue())


if __name__ == "__main__":
    unittest.main()
