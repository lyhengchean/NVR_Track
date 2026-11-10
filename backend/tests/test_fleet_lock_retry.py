"""Conservative recovery from per-NVR lock conflicts in the fleet runner."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nvr_track.web import dashboard
from nvr_track.cli import run_all_nvr_reports as fleet


class FleetLockRetryTests(unittest.TestCase):
    @staticmethod
    def locked(nvr_id="nvr-04"):
        return subprocess.CompletedProcess(
            ["report"], returncode=2,
            stdout=f"Another report is already running for {nvr_id}; no queries sent.\n",
            stderr="",
        )

    @staticmethod
    def finished(code=1, stdout="JSON report saved: /app/reports/report.json\n"):
        return subprocess.CompletedProcess(["report"], returncode=code,
                                           stdout=stdout, stderr="")

    @patch.object(fleet.time, "sleep")
    @patch.object(fleet.subprocess, "run")
    def test_two_lock_conflicts_then_success(self, run, sleep):
        success = self.finished()
        run.side_effect = [self.locked(), self.locked(), success]
        result = fleet.run_report_with_lock_retry(["python", "-m", "report"],
                                                   "nvr-04", 420)
        self.assertIs(result, success)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_any_call(5)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 420)

    @patch.object(fleet.time, "sleep")
    @patch.object(fleet.subprocess, "run")
    def test_lock_exhaustion_is_bounded(self, run, sleep):
        run.return_value = self.locked()
        result = fleet.run_report_with_lock_retry(["python"], "nvr-04", 420)
        self.assertTrue(fleet.is_lock_conflict(result, "nvr-04"))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch.object(fleet.time, "sleep")
    @patch.object(fleet.subprocess, "run")
    def test_non_lock_failures_are_never_retried(self, run, sleep):
        for result in (
            self.finished(),  # warning or critical is not a failure to retry
            self.finished(2, "Credential missing\n"),
            self.finished(2, "Another report is already running for nvr-02; no queries sent.\n"),
            self.finished(2, "Another report is already running for nvr-04; no queries sent.\nOther diagnostics\n"),
        ):
            with self.subTest(stdout=result.stdout):
                run.reset_mock()
                run.return_value = result
                self.assertIs(fleet.run_report_with_lock_retry(["python"], "nvr-04", 420), result)
                run.assert_called_once()
                sleep.assert_not_called()

    @patch.object(fleet.time, "sleep")
    @patch.object(fleet.subprocess, "run", side_effect=subprocess.TimeoutExpired("test", 420))
    def test_timeout_is_not_retried(self, run, sleep):
        with self.assertRaises(subprocess.TimeoutExpired):
            fleet.run_report_with_lock_retry(["python"], "nvr-04", 420)
        run.assert_called_once()
        sleep.assert_not_called()

    @patch.object(fleet.time, "sleep")
    @patch.object(fleet.subprocess, "run")
    def test_remaining_conflict_is_explicitly_recorded_for_dashboard(self, run, sleep):
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            run.return_value = self.locked()
            nvr = SimpleNamespace(id="nvr-04", ip="198.51.100.4", http_port=80)
            with (patch.object(fleet, "REPORTS_DIR", report_dir),
                  patch.object(dashboard, "REPORTS_DIR", report_dir),
                  patch.object(fleet, "load_inventory", return_value=[nvr]),
                  patch.object(fleet, "send_email_report", return_value=(False, "SKIPPED"))):
                code = fleet.main()
                self.assertEqual(code, 2)
                saved = json.loads((report_dir / "fleet_latest_run.json").read_text())
                self.assertEqual(saved["devices"], [{"id": "nvr-04", "result": "ERROR", "reason": "LOCK_CONFLICT"}])
                self.assertNotIn("198.51.100.4", str(saved))
                self.assertEqual(dashboard._read_fleet_run_state()[0], {"nvr-04": "LOCK_CONFLICT"})
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
