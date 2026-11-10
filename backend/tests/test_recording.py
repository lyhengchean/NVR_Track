"""Unit tests for recording-search response parsing."""

import unittest

from nvr_track.nvr.recording import make_search_xml, parse_search_response
from datetime import datetime, timezone
import xml.etree.ElementTree as ET


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


class RecordingTests(unittest.TestCase):
    def test_parse_match_without_total_matches(self):
        xml = b"""<?xml version="1.0"?>
        <CMSearchResult xmlns="http://www.isapi.org/ver20/XMLSchema">
          <responseStatus>true</responseStatus>
          <responseStatusStrg>OK</responseStatusStrg>
          <numOfMatches>1</numOfMatches>
          <matchList>
            <searchMatchItem>
              <trackID>201</trackID>
              <timeSpan>
                <startTime>2026-10-22T08:00:00Z</startTime>
                <endTime>2026-10-22T09:00:00Z</endTime>
              </timeSpan>
            </searchMatchItem>
          </matchList>
        </CMSearchResult>"""

        result = parse_search_response(xml)

        self.assertEqual(result["num_matches"], 1)
        self.assertIsNone(result["total_matches"])
        self.assertEqual(result["matches"][0]["track_id"], 201)

    def test_parse_reports_total_matches_when_present(self):
        xml = b"""<CMSearchResult>
          <responseStatus>true</responseStatus>
          <responseStatusStrg>OK</responseStatusStrg>
          <numOfMatches>0</numOfMatches>
          <totalMatches>0</totalMatches>
        </CMSearchResult>"""

        result = parse_search_response(xml)

        self.assertEqual(result["total_matches"], 0)
        self.assertEqual(result["num_matches"], 0)

    def test_reject_count_mismatch(self):
        xml = b"""<CMSearchResult>
          <responseStatus>true</responseStatus>
          <responseStatusStrg>OK</responseStatusStrg>
          <numOfMatches>1</numOfMatches>
        </CMSearchResult>"""

        with self.assertRaisesRegex(ValueError, "number of returned"):
            parse_search_response(xml)

    def test_request_contains_correct_legacy_field(self):
        start = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

        root = ET.fromstring(make_search_xml(201, start, end, 10))
        names = {local_name(node.tag) for node in root.iter()}

        self.assertIn("trackList", names)
        self.assertNotIn("trackIDList", names)
        self.assertIn("searchResultPostion", names)
        self.assertNotIn("searchResultPosition", names)
        self.assertNotIn("contentTypeList", names)

    def test_reject_invalid_xml(self):
        with self.assertRaisesRegex(ValueError, "valid XML"):
            parse_search_response(b"<CMSearchResult>")


if __name__ == "__main__":
    unittest.main()