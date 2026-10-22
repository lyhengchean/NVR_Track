"""Tests for conservative overall monitoring status classification."""

import unittest

from app.reporting import determine_overall_status


def healthy_inputs():
    return {
        "device_info_status": "DEVICE_INFO_RETRIEVED",
        "inventory_status": "CHANNELS_RETRIEVED",
        "channel_status": "CHANNEL_STATUS_RETRIEVED",
        "storage_status": "HEALTHY",
        "offline_count": 0,
        "expected_count": 56,
        "actual_count": 56,
        "recording_status": "RECORDING_EVIDENCE_FOUND",
    }


class OverallStatusTests(unittest.TestCase):
    def test_all_verified_checks_can_be_healthy(self):
        result = determine_overall_status(**healthy_inputs())
        self.assertEqual(result["status"], "HEALTHY")

    def test_critical_storage_overrides_other_results(self):
        inputs = healthy_inputs()
        inputs["storage_status"] = "CRITICAL"
        result = determine_overall_status(**inputs)
        self.assertEqual(result["status"], "CRITICAL")

    def test_offline_channels_are_warning(self):
        inputs = healthy_inputs()
        inputs["offline_count"] = 3
        result = determine_overall_status(**inputs)
        self.assertEqual(result["status"], "WARNING")

    def test_unverified_recording_cannot_be_healthy(self):
        inputs = healthy_inputs()
        inputs["recording_status"] = "UNKNOWN"
        result = determine_overall_status(**inputs)
        self.assertEqual(result["status"], "UNKNOWN")

    def test_unavailable_channel_status_is_unknown(self):
        inputs = healthy_inputs()
        inputs["channel_status"] = "TIMEOUT"
        result = determine_overall_status(**inputs)
        self.assertEqual(result["status"], "UNKNOWN")

    def test_channel_count_mismatch_is_warning(self):
        inputs = healthy_inputs()
        inputs["actual_count"] = 55
        result = determine_overall_status(**inputs)
        self.assertEqual(result["status"], "WARNING")


if __name__ == "__main__":
    unittest.main()