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

    def test_critical_storage_reason_does_not_claim_every_issue_is_disk_failure(self):
        inputs = healthy_inputs()
        inputs["storage_status"] = "CRITICAL"
        result = determine_overall_status(**inputs)
        self.assertEqual(result["status"], "CRITICAL")
        self.assertTrue(any("expected disks missing" in item for item in result["critical_reasons"]))

    def test_storage_warning_has_actionable_explanation(self):
        inputs = healthy_inputs()
        inputs["storage_status"] = "WARNING"
        result = determine_overall_status(**inputs)
        self.assertEqual(result["status"], "WARNING")
        self.assertTrue(any("overwrite/quota" in item for item in result["warnings"]))

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

    def test_event_recording_gap_is_not_automatically_a_failure(self):
        inputs = healthy_inputs()
        inputs["recording_status"] = "NO_MATCHES_IN_WINDOW"
        inputs["per_camera_summary"] = {
            "enabled": True, "no_recent_evidence": 1,
            "missing_expected": 0, "online_missing_expected": 0,
            "search_errors": 0, "inconclusive": 0,
            "track_mapping_problems": 0, "not_checked": 0,
        }
        result = determine_overall_status(**inputs)
        self.assertEqual(result["status"], "HEALTHY")

    def test_online_continuous_recording_gap_triggers_warning(self):
        inputs = healthy_inputs()
        inputs["per_camera_summary"] = {
            "enabled": True, "no_recent_evidence": 1,
            "missing_expected": 1, "online_missing_expected": 1,
            "search_errors": 0, "inconclusive": 0,
            "track_mapping_problems": 0, "not_checked": 0,
        }
        result = determine_overall_status(**inputs)
        self.assertEqual(result["status"], "WARNING")
        self.assertTrue(any("ONLINE" in text for text in result["warnings"]))


if __name__ == "__main__":
    unittest.main()