"""Settings that fill every camera column on the dashboard."""
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_nvr_report as runner

ROOT = Path(__file__).resolve().parent.parent


class DefaultExpectationTests(unittest.TestCase):
    def test_unset_stays_unspecified(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(runner.default_recording_expectation(), "unspecified")

    def test_continuous_and_case_insensitive(self):
        with patch.dict("os.environ", {"NVR_DEFAULT_RECORDING_EXPECTATION": " Continuous "}):
            self.assertEqual(runner.default_recording_expectation(), "continuous")

    def test_invalid_value_is_rejected(self):
        with patch.dict("os.environ", {"NVR_DEFAULT_RECORDING_EXPECTATION": "always"}):
            self.assertEqual(runner.default_recording_expectation(), "unspecified")


class DashboardLabelTests(unittest.TestCase):
    def test_recording_cameras_show_history_not_needed(self):
        html = (ROOT / "app" / "dashboard_ui.html").read_text(encoding="utf-8")
        self.assertIn('HISTORICAL_NOT_NEEDED: "Not needed (recording now)"', html)
        self.assertIn('pill(notNeeded(ch) ? "HISTORICAL_NOT_NEEDED" : h(ch).status)', html)


class EnvExampleTests(unittest.TestCase):
    def test_calendar_limit_covers_largest_nvr_in_two_runs(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        limit = int(re.search(r"^NVR_RETENTION_MAX_CAMERAS_PER_RUN=(\d+)$", text, re.M).group(1))
        self.assertGreaterEqual(limit * 2, 56)
        self.assertIn("NVR_DEFAULT_RECORDING_EXPECTATION=continuous", text)


if __name__ == "__main__":
    unittest.main()
