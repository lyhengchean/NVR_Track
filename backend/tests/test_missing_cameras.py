"""Expected cameras without a row get their real reason (10 Nov 2026 deploy:
nvr-04 answered with 45 of 46 channels, but the dashboard and the deploy check
said "NVR unreachable"). Also the deploy check's event-stream list."""
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frontend_source import DASHBOARD_UI
from nvr_track.core.paths import PROJECT_ROOT
from nvr_track.nvr import events
from nvr_track.web import dashboard

SCRIPT = PROJECT_ROOT / "scripts" / "deploy_check.sh"


def heredoc(start: str) -> str:
    """The Python block of deploy_check.sh that follows the line containing `start`."""
    text = SCRIPT.read_text(encoding="utf-8")
    after = text.split(start, 1)[1].split("\n", 1)[1]
    return after.split("\nPY\n", 1)[0]


class MissingCameraTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patcher = patch.object(dashboard, "REPORTS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        # nvr-04 answered: 45 channels configured, the inventory expects 46.
        self.write("nvr-04", rows=45, expected=46, configured=45)
        # nvr-08 did not answer at all.
        self.write("nvr-08", rows=0, expected=24, configured=None, device="TIMEOUT")
        # nvr-02: channel inventory failed, the status list named 1 of 2 channels.
        self.write("nvr-02", rows=1, expected=2, configured=None)
        self.write("nvr-05", rows=16, expected=16, configured=16)

    def write(self, nvr_id, rows, expected, configured, device="DEVICE_INFO_RETRIEVED"):
        report = {"schema_version": 2, "nvr": {"id": nvr_id, "ip": "192.0.2.1"},
                  "check_completed_at": "2026-11-10T08:46:31+07:00",
                  "device_info": {"status": device}, "overall": {"status": "WARNING"},
                  "channel_status": {"online_count": rows, "offline_count": 0},
                  "channel_inventory": {"expected_count": expected, "configured_count": configured,
                                        "channels": [{"channel_id": str(c), "reported_status": "ONLINE",
                                                      "recording_result": {"status": "RECORDING"}}
                                                     for c in range(1, rows + 1)]},
                  "storage": {"status": "HEALTHY", "disks": []}}
        (self.root / f"{nvr_id}_20261110T084631+0700.json").write_text(json.dumps(report), encoding="utf-8")

    def test_reason_per_nvr_and_in_the_summary(self):
        data = dashboard.read_dashboard_data()
        missing = {n["id"]: n["missing_cameras"] for n in data["nvrs"]}
        self.assertEqual(missing["nvr-04"], {"not_configured": 1, "not_listed": 0})
        self.assertEqual(missing["nvr-08"], {"not_configured": 0, "not_listed": 24})
        self.assertEqual(missing["nvr-02"], {"not_configured": 0, "not_listed": 1})
        self.assertEqual(missing["nvr-05"], {"not_configured": 0, "not_listed": 0})
        s = data["summary"]
        self.assertEqual((s["cameras_without_result"], s["cameras_not_listed"], s["cameras_not_configured"]),
                         (26, 25, 1))

    def test_rules(self):
        def case(rows, expected, configured):
            return dashboard.missing_cameras({"channels": [{}] * rows, "expected_channels": expected,
                                              "configured_channels": configured})
        self.assertEqual(case(45, 46, 45), {"not_configured": 1, "not_listed": 0})
        self.assertEqual(case(46, 46, 46), {"not_configured": 0, "not_listed": 0})
        self.assertEqual(case(47, 46, 47), {"not_configured": 0, "not_listed": 0})   # More than expected.
        self.assertEqual(case(0, 24, None), {"not_configured": 0, "not_listed": 24})
        self.assertEqual(case(3, 5, None), {"not_configured": 0, "not_listed": 2})
        self.assertEqual(case(0, None, None), {"not_configured": 0, "not_listed": 0})
        self.assertEqual(case(0, 4, True), {"not_configured": 0, "not_listed": 4})    # Not a number.

    def test_page_names_the_real_reason(self):
        self.assertIn("s.cameras_not_configured", DASHBOARD_UI)
        self.assertIn("not configured on its NVR (inventory mismatch)", DASHBOARD_UI)
        self.assertIn('["No result (NVR unreachable)", num(s.cameras_not_listed)', DASHBOARD_UI)
        self.assertIn('cell.dataset.ghost === "config"', DASHBOARD_UI)
        self.assertNotIn("num(s.cameras_without_result)", DASHBOARD_UI)

    def test_deploy_check_names_the_reason(self):
        if not SCRIPT.exists():
            self.skipTest("deploy_check.sh is not part of this test image")
        code = heredoc("DASH=$(docker compose exec -T nvr-dashboard python - 2>&1 <<'PY'")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(code, "deploy_check section 6", "exec"), {})
        lines = out.getvalue().splitlines()
        self.assertIn("CHECK missing=nvr-04 config 1 45 46", lines)
        self.assertIn("CHECK missing=nvr-08 unlisted 24 - 24", lines)
        self.assertIn("CHECK missing=nvr-02 unlisted 1 - 2", lines)
        self.assertIn("CHECK incomplete_nvrs=3", lines)
        self.assertTrue(any(re.match(r"\s+cameras_not_configured\s+1$", line) for line in lines))
        self.assertTrue(any(re.match(r"\s+smart_problems\s+0$", line) for line in lines))
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('config) warn "$ID has $A channels configured, the inventory expects $B', text)
        self.assertIn("set expected_channels to $A for $ID in config/nvrs.local.json", text)
        self.assertNotIn("NVR unreachable; see section 9", text)


class EventStreamListTests(unittest.TestCase):
    def test_deploy_check_lists_nvrs_without_event_streams(self):
        if not SCRIPT.exists():
            self.skipTest("deploy_check.sh is not part of this test image")
        code = heredoc("STREAMS=$(docker compose exec -T nvr-events python - 2>/dev/null <<'PY'")
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "config").mkdir()
            shutil.copy(PROJECT_ROOT / "config" / "nvrs.example.json", Path(temp) / "config" / "nvrs.local.json")
            status = {"nvrs": {f"nvr-{n:02d}": {"connected": n != 2, "last_error": "TIMEOUT"}
                               for n in range(1, 13) if n != 11}}
            out = io.StringIO()
            cwd = os.getcwd()
            try:
                os.chdir(temp)
                with patch.object(events, "read_status", return_value=status), contextlib.redirect_stdout(out):
                    exec(compile(code, "deploy_check events", "exec"), {})
            finally:
                os.chdir(cwd)
        lines = out.getvalue().splitlines()
        self.assertEqual([line for line in lines if line.startswith("OFF ")], ["OFF nvr-11"])
        self.assertEqual(sum(line.startswith("UP ") for line in lines), 10)
        self.assertTrue(any(line.startswith("DOWN nvr-02 ") for line in lines))
        self.assertIn('echo "  No real-time events (not in NVR_EVENT_NVR_IDS): $OFF"',
                      SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
