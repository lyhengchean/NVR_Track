"""Brief NVR outages must not make cameras disappear or look healthy."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nvr_track.web import dashboard
from nvr_track.nvr.isapi import ChannelInfo, ChannelInventoryResult, ChannelStatus, ChannelStatusResult
from nvr_track.nvr.storage import StorageCheck
from nvr_track.cli import run_nvr_report as runner
from frontend_source import DASHBOARD_UI


class ChannelSourceTests(unittest.TestCase):
    def test_inventory_is_used_when_it_was_retrieved(self):
        inventory = ChannelInventoryResult("CHANNELS_RETRIEVED", 200, (ChannelInfo("1", "Gate", "192.0.2.10"),))
        status = ChannelStatusResult("CHANNEL_STATUS_RETRIEVED", 200, (ChannelStatus("1", True),))
        channels, source = runner.channel_sources(inventory, status)
        self.assertEqual(source, "CHANNEL_INVENTORY")
        self.assertEqual(channels[0].name, "Gate")

    def test_status_list_keeps_cameras_when_inventory_timed_out(self):
        inventory = ChannelInventoryResult("TIMEOUT", None, (), "The request timed out.")
        status = ChannelStatusResult("CHANNEL_STATUS_RETRIEVED", 200,
                                     (ChannelStatus("1", True), ChannelStatus("2", False)))
        channels, source = runner.channel_sources(inventory, status)
        self.assertEqual(source, "CHANNEL_STATUS_FALLBACK")
        self.assertEqual([c.channel_id for c in channels], ["1", "2"])
        self.assertIsNone(channels[0].name)
        self.assertIsNone(channels[0].camera_ip)

    def test_no_channels_when_both_requests_failed(self):
        inventory = ChannelInventoryResult("TIMEOUT")
        status = ChannelStatusResult("TIMEOUT")
        self.assertEqual(runner.channel_sources(inventory, status), ([], "UNAVAILABLE"))

    def test_storage_retry_only_for_network_failures(self):
        self.assertTrue(runner.storage_unreachable(
            StorageCheck("UNKNOWN", None, (), "Network request failed or timed out.")))
        self.assertFalse(runner.storage_unreachable(
            StorageCheck("UNKNOWN", 401, (), "Authentication failed.")))
        self.assertFalse(runner.storage_unreachable(
            StorageCheck("CRITICAL", 200, (), "No disks.")))


class DashboardUnreachableTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write(self, nvr_id, channels, expected, device):
        report = {"schema_version": 2, "nvr": {"id": nvr_id, "ip": "192.0.2.1"},
                  "check_completed_at": "2026-10-30T09:57:00+07:00",
                  "device_info": device, "overall": {"status": "CRITICAL"},
                  "channel_status": {"online_count": 0, "offline_count": 0, "unknown_count": 0},
                  "channel_inventory": {"expected_count": expected, "channels": channels},
                  "storage": {"status": "UNKNOWN", "disks": []}}
        (self.root / f"{nvr_id}_20261030T095700+0700.json").write_text(json.dumps(report), encoding="utf-8")

    def test_unreachable_nvr_cameras_are_counted_and_none_is_not_shown(self):
        self.write("nvr-08", [], 24, {"status": "TIMEOUT", "model": None, "firmware_version": None})
        self.write("nvr-02", [{"channel_id": "1", "name": None, "camera_ip": None, "reported_status": "ONLINE",
                               "recording_result": {"status": "RECORDING", "track_id": 101}}],
                   2, {"model": "DS-7716NI-I4", "firmware_version": "V4.61.025"})
        with patch.object(dashboard, "REPORTS_DIR", self.root):
            data = dashboard.read_dashboard_data()
        self.assertEqual(data["summary"]["cameras_without_result"], 24 + 1)
        unreachable = next(n for n in data["nvrs"] if n["id"] == "nvr-08")
        self.assertEqual(unreachable["model"], "")
        self.assertEqual(unreachable["firmware_version"], "")
        fallback_row = next(n for n in data["nvrs"] if n["id"] == "nvr-02")["channels"][0]
        self.assertEqual((fallback_row["name"], fallback_row["camera_ip"]), ("", ""))
        self.assertIn("cameras_without_result", DASHBOARD_UI)


if __name__ == "__main__":
    unittest.main()
