"""Duplicate cameras: one camera on several channels, or one name on several
channels. Report, dashboard, Telegram and email; end-to-end against a fake NVR."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_nvr import FakeNVR

from app import dashboard
from app import telegram_views as views
from app.duplicates import find_duplicates, notes_by_channel, warnings
from app.isapi import parse_channel_inventory
from app.reporting import determine_overall_status
from scripts import run_nvr_report as runner
from scripts.run_all_nvr_reports import camera_conditions


def cam(cid, ip="", name="", source=None):
    return {"channel_id": str(cid), "camera_ip": ip, "name": name, "source_input": source}


class FindDuplicatesTests(unittest.TestCase):
    def test_same_ip_and_input_is_one_camera_twice(self):
        result = find_duplicates([cam(21, "10.0.0.117", "Kiln A", 1), cam(3, "10.0.0.3", "Gate", 1),
                                  cam(51, "10.0.0.117", "Kiln B", 1)])
        self.assertEqual(result["same_camera"], [{"channels": ["21", "51"]}])
        self.assertEqual(result["same_name"], [])

    def test_multi_lens_camera_or_encoder_inputs_are_not_duplicates(self):
        result = find_duplicates([cam(1, "10.0.0.9", "Lens 1", 1), cam(2, "10.0.0.9", "Lens 2", 2)])
        self.assertEqual(result["same_camera"], [])

    def test_empty_addresses_are_ignored_and_names_compare_loosely(self):
        result = find_duplicates([cam(20, "0.0.0.0", "Cement Silo #1"), cam(22, "", " cement  silo #1 "),
                                  cam(9, "", "Gate")])
        self.assertEqual(result["same_camera"], [])
        self.assertEqual(result["same_name"], [{"name": "Cement Silo #1", "channels": ["20", "22"]}])

    def test_unretrieved_channel_list_is_not_checked(self):
        self.assertEqual(find_duplicates(None)["status"], "NOT_CHECKED")

    def test_messages_never_contain_ip_addresses(self):
        result = find_duplicates([cam(21, "10.0.0.117", "Silo", 1), cam(51, "10.0.0.117", "Silo", 1)])
        text = " ".join(warnings(result))
        self.assertNotIn("10.0.0.117", text)
        self.assertIn("ch21 = ch51", text)
        self.assertIn("“Silo” (ch21, ch51)", text)
        self.assertEqual(notes_by_channel(result)["51"], ["Same camera as ch21", "Same name as ch21"])

    def test_duplicates_make_an_otherwise_healthy_nvr_a_warning(self):
        base = {"device_info_status": "DEVICE_INFO_RETRIEVED", "inventory_status": "CHANNELS_RETRIEVED",
                "channel_status": "CHANNEL_STATUS_RETRIEVED", "storage_status": "HEALTHY", "offline_count": 0,
                "expected_count": 2, "actual_count": 2, "recording_status": "RECORDING_EVIDENCE_FOUND"}
        self.assertEqual(determine_overall_status(**base)["status"], "HEALTHY")
        dup = find_duplicates([cam(1, "", "Gate"), cam(2, "", "Gate")])
        result = determine_overall_status(**base, duplicates=dup)
        self.assertEqual(result["status"], "WARNING")
        self.assertTrue(result["warnings"][0].startswith("Channel names used more than once"))

    def test_parser_reads_the_camera_input(self):
        channels = parse_channel_inventory(
            b"<InputProxyChannelList><InputProxyChannel><id>1</id><sourceInputPortDescriptor>"
            b"<ipAddress>10.0.0.9</ipAddress><srcInputPort>2</srcInputPort></sourceInputPortDescriptor>"
            b"</InputProxyChannel><InputProxyChannel><id>2</id></InputProxyChannel></InputProxyChannelList>")
        self.assertEqual([c.source_input for c in channels], [2, None])


class EndToEndDuplicateTests(unittest.TestCase):
    """Real reporter against the fake NVR, then dashboard, Telegram and email."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("reports", "notes", "events", "snapshots", "scan", "config"):
            (self.root / name).mkdir()
        self.nvr = FakeNVR()
        self.addCleanup(self.nvr.close)
        (self.root / "config" / "nvrs.local.json").write_text(json.dumps({"schema_version": 1, "nvrs": [
            {"id": "nvr-01", "name": "Kiln", "ip": "127.0.0.1", "http_port": self.nvr.port,
             "expected_channels": 4}]}))
        patches = [patch.object(runner, "ROOT", self.root),
                   patch.object(runner, "INVENTORY_PATH", self.root / "config" / "nvrs.local.json"),
                   patch.object(dashboard, "REPORTS_DIR", self.root / "reports"),
                   patch.object(dashboard, "SCAN_STATE_DIR", self.root / "scan"),
                   patch.dict("os.environ", {
                       "NVR_USERNAME": "admin", "NVR_PASSWORD": "not-a-real-password",
                       "NVR_PER_CAMERA_RECORDING_IDS": "nvr-01", "NVR_RECORDING_RETRY_DELAY_SECONDS": "0",
                       "NOTES_DIR": str(self.root / "notes"), "EVENTS_DIR": str(self.root / "events"),
                       "SNAPSHOT_DIR": str(self.root / "snapshots")})]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_duplicates_reach_report_dashboard_telegram_and_email(self):
        self.nvr.camera_ips["3"] = "192.0.2.1"                   # ch3 pulls ch1's camera.
        self.nvr.channels["2"] = ("Gate", True)                  # Two channels named Gate.
        with patch.object(sys, "argv", ["run_nvr_report", "--ip", "127.0.0.1", "--http-port", str(self.nvr.port)]), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            runner.main()
        self.assertIn("Warning: Same camera on more than one channel: ch1 = ch3.", out.getvalue())
        report = json.loads(next((self.root / "reports").glob("nvr-01_*.json")).read_text(encoding="utf-8"))
        self.assertEqual(report["duplicates"]["same_camera"], [{"channels": ["1", "3"]}])
        self.assertEqual(report["duplicates"]["same_name"], [{"name": "Gate", "channels": ["1", "2"]}])
        self.assertEqual(report["channel_inventory"]["channels"][0]["source_input"], 1)
        self.assertNotIn("192.0.2.1", " ".join(report["overall"]["warnings"]))
        # Read-only: the only POSTs are recording searches.
        self.assertTrue(all(p == "/ISAPI/ContentMgmt/search" for m, p in self.nvr.requests if m == "POST"))

        data = dashboard.read_dashboard_data()
        cams = {c["channel_id"]: c for c in data["nvrs"][0]["channels"]}
        self.assertEqual(cams["3"]["duplicate_notes"], ["Same camera as ch1"])
        self.assertEqual(cams["1"]["duplicate_notes"], ["Same camera as ch3", "Same name as ch2"])
        self.assertIn("ch.duplicate_notes", dashboard.HTML)
        self.assertIn("⚠️ Same camera as ch1", views.camera_search(data, "nvr-01 3", show_ips=False))
        self.assertTrue(any("Same camera on more than one channel" in f
                            for f in camera_conditions(report)["findings"]))


if __name__ == "__main__":
    unittest.main()
