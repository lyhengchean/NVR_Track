"""Scan reports count the drop-outs the live checker saw (19 Nov 2026).

nvr-06 (ch16, ch18) and nvr-08 (ch8-ch10) were HEALTHY in the scan and the
daily email but WARNING on the dashboard and in Telegram: the live checks
(every 60 s) saw them go offline and online, the 2-hourly scans did not.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fake_nvr import FakeNVR

from nvr_track.cli import run_nvr_report as runner
from nvr_track.core.paths import PROJECT_ROOT
from nvr_track.monitoring import live

UNSTABLE = "1 camera keeps going offline and online (4+ changes in 24 h): ch2."


def ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class ScanCountsLiveDropOutsTests(unittest.TestCase):
    def scan(self, changes: dict | None) -> tuple[dict, str]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name in ("reports", "notes", "events", "snapshots", "config", "state/live"):
            (root / name).mkdir(parents=True)
        nvr = FakeNVR()
        self.addCleanup(nvr.close)
        (root / "config" / "nvrs.local.json").write_text(json.dumps({"schema_version": 1, "nvrs": [
            {"id": "nvr-01", "ip": "127.0.0.1", "http_port": nvr.port, "expected_channels": 4}]}))
        if changes is not None:  # As nvr-live writes it; the scanner reads it at the default path.
            live.write_entry({"schema_version": live.SCHEMA, "nvr": "nvr-01", "changes": changes},
                             root / "state" / "live")
        out = io.StringIO()
        with patch.object(runner, "ROOT", root), \
                patch.object(runner, "INVENTORY_PATH", root / "config" / "nvrs.local.json"), \
                patch.dict("os.environ", {"NVR_USERNAME": "admin", "NVR_PASSWORD": "not-a-real-password",
                                          "NVR_RECORDING_RETRY_DELAY_SECONDS": "0",
                                          "NOTES_DIR": str(root / "notes"), "EVENTS_DIR": str(root / "events"),
                                          "SNAPSHOT_DIR": str(root / "snapshots")}), \
                patch.object(sys, "argv", ["run_nvr_report", "--ip", "127.0.0.1", "--http-port", str(nvr.port)]), \
                patch("sys.stdout", out):
            os.environ.pop("LIVE_DIR", None)  # The scanner has none set (restored on exit).
            runner.main()
        report = json.loads(next((root / "reports").glob("nvr-01_*.json")).read_text(encoding="utf-8"))
        return report, out.getvalue()

    def test_live_drop_outs_count_in_the_scan_report(self):
        report, out = self.scan({"2": [ago(5), ago(4), ago(3), ago(2), ago(1)]})
        self.assertEqual([(c["channel_id"], c["changes"]) for c in report["flapping"]["cameras"]], [("2", 5)])
        self.assertTrue(any(w.startswith(UNSTABLE) for w in report["overall"]["warnings"]))
        self.assertIn(report["overall"]["status"], {"WARNING", "CRITICAL"})
        self.assertIn(f"Warning: {UNSTABLE}", out)                       # Deploy check section 5.
        # The dashboard overlay on top: still one line, updated when the live checks see more.
        times = [ago(5), ago(4), ago(3), ago(2), ago(1)]
        for changes, listed in (({"2": times}, "ch2."), ({"2": times, "3": times}, "ch2, ch3.")):
            shown = live.overlay(report, {"schema_version": live.SCHEMA, "nvr": "nvr-01", "changes": changes})
            lines = [w for w in shown["overall"]["warnings"] if "going offline and online" in w]
            self.assertEqual(len(lines), 1, lines)
            self.assertIn(listed, lines[0])

    def test_old_or_missing_live_data_adds_nothing(self):
        for changes in (None, {}, {"2": [ago(30), ago(29), ago(28), ago(27), ago(26)]}, {"9": [ago(1)] * 5}):
            with self.subTest(changes=changes):
                report, out = self.scan(changes)
                self.assertEqual(report["flapping"]["cameras"], [])
                self.assertNotIn("going offline and online", out)

    def test_scanner_and_report_services_read_the_live_state(self):
        path = PROJECT_ROOT / "compose.yaml"
        if not path.exists():
            self.skipTest("compose.yaml is not part of this test image")
        text = path.read_text(encoding="utf-8")
        for name in ("nvr-report", "nvr-report-all", "nvr-scanner"):
            block = text.split(f"  {name}:\n", 1)[1].split("\n  nvr-", 1)[0]
            self.assertIn("- ./state/live:/app/state/live:ro", block, name)   # Read-only.


if __name__ == "__main__":
    unittest.main()
