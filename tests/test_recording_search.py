import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from scripts.test_recording_search import make_search_xml


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


class RecordingSearchRequestTests(unittest.TestCase):
    def test_request_uses_verified_legacy_format(self):
        start = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

        root = ET.fromstring(make_search_xml(201, start, end, 10))
        fields = {
            local_name(element.tag)
            for element in root.iter()
        }

        self.assertIn("trackList", fields)
        self.assertIn("searchResultPostion", fields)
        self.assertNotIn("searchResultPosition", fields)
        self.assertNotIn("trackIDList", fields)
        self.assertNotIn("contentTypeList", fields)

        values = {
            local_name(element.tag): element.text
            for element in root.iter()
            if element.text
        }

        self.assertEqual(values["trackID"], "201")
        self.assertEqual(values["startTime"], "2026-10-22T08:00:00Z")
        self.assertEqual(values["endTime"], "2026-10-22T09:00:00Z")
        self.assertEqual(values["maxResults"], "10")

    def test_request_preserves_track_and_time_window(self):
        start = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

        root = ET.fromstring(make_search_xml(201, start, end, 10))

        values = {
            local_name(element.tag): element.text
            for element in root.iter()
            if element.text
        }

        self.assertEqual(values["trackID"], "201")
        self.assertEqual(values["startTime"], "2026-10-22T08:00:00Z")
        self.assertEqual(values["endTime"], "2026-10-22T09:00:00Z")
        self.assertEqual(values["maxResults"], "10")


if __name__ == "__main__":
    unittest.main()