"""A rejected search reply says which rule failed, in the report and in the
read-only explain tool (nvr-01 ch47 "Elevator Exit": "could not be validated")."""
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fake_nvr import FakeNVR

from app.recording import check_recording_evidence
from scripts import explain_recording_search as explain

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
