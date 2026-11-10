"""Cameras recorded by more than one NVR (same IP and input), as information:
dashboard notes and findings, Telegram /camera and the email. No network."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_telegram_bot import Fixture, channel

from nvr_track.web import dashboard
from nvr_track.monitoring.duplicates import fleet_info, fleet_notes, fleet_text, find_fleet_duplicates
from nvr_track.cli import run_all_nvr_reports as fleet
from frontend_source import DASHBOARD_UI


def cam(cid, ip, name, source=1):
    return {"channel_id": str(cid), "name": name, "camera_ip": ip, "source_input": source}


# Shapes from the fleet on 05 Nov (addresses changed).
FLEET = [
    ("nvr-01", [cam(20, "198.51.100.149", "Cement Silo #1"), cam(25, "198.51.100.97", "Pre-Heater 7F"),
                cam(44, "198.51.100.23", "Workshop 3"), cam(1, "192.168.254.2", "Gate"),
                cam(5, "198.51.100.60", "Encoder A", 1), cam(21, "198.51.100.70", "Packing"),
                cam(51, "198.51.100.70", "Packing")]),
    ("nvr-03", [cam(24, "198.51.100.149", "Cement Silo #1")]),
    ("nvr-04", [cam(21, "198.51.100.149", "Cement Silo #1"), cam(19, "198.51.100.97", "Pre-Heater 7F"),
                cam(1, "192.168.254.2", "Yard"), cam(6, "198.51.100.60", "Encoder B", 2), cam(9, "0.0.0.0", "x")]),
    ("nvr-12", [cam(42, "198.51.100.23", "IPCamera 42")]),
]


class FindTests(unittest.TestCase):
    def test_groups_across_nvrs_only(self):
        groups = find_fleet_duplicates(FLEET)
        self.assertEqual(fleet_text(groups), ["nvr-01 ch20 = nvr-03 ch24 = nvr-04 ch21",
                                              "nvr-01 ch25 = nvr-04 ch19",
                                              "nvr-01 ch44 = nvr-12 ch42"])
        # Not matched: PoE switch addresses (192.168.254.x), other encoder input,
        # 0.0.0.0, and a camera twice on one NVR (that is the per-NVR warning).

    def test_notes_and_info(self):
        groups = find_fleet_duplicates(FLEET)
        notes = fleet_notes(groups)
        self.assertEqual(notes[("nvr-01", "20")], ["Also recorded by nvr-03 ch24, nvr-04 ch21"])
        self.assertEqual(notes[("nvr-01", "44")], ["Also recorded by nvr-12 ch42 (named “IPCamera 42” there)"])
        self.assertEqual(notes[("nvr-12", "42")], ["Also recorded by nvr-01 ch44 (named “Workshop 3” there)"])
        self.assertNotIn(("nvr-01", "1"), notes)
        info = fleet_info(groups, "nvr-01")
        self.assertEqual(info, ["Cameras also recorded by another NVR: ch20 = nvr-03 ch24, nvr-04 ch21; "
                                "ch25 = nvr-04 ch19; ch44 = nvr-12 ch42. Fine if intended; it uses disk space on both."])
        self.assertEqual(fleet_info(groups, "nvr-03"), ["Cameras also recorded by another NVR: ch24 = nvr-01 ch20, "
                                                        "nvr-04 ch21. Fine if intended; it uses disk space on both."])
        self.assertEqual(fleet_info(groups, "nvr-09"), [])
        self.assertFalse(any("198.51" in line for line in info + fleet_text(groups)))      # No IPs in text.
        self.assertEqual(find_fleet_duplicates([("nvr-01", None), ("nvr-02", ["junk"])]), [])


class ShownTests(Fixture):
    def test_dashboard_and_telegram(self):
        self.report("nvr-01", channels=[channel(1, name="Silo"), channel(2)])
        self.report("nvr-04", channels=[channel(1, name="Silo top"), channel(7)])   # ch1: same 192.0.2.1.
        data = dashboard.read_dashboard_data()
        self.assertEqual(fleet_text(data["fleet_duplicates"]), ["nvr-01 ch1 = nvr-04 ch1"])
        nvrs = {n["id"]: n for n in data["nvrs"]}
        self.assertIn("Cameras also recorded by another NVR: ch1 = nvr-04 ch1.", nvrs["nvr-01"]["info"][-1])
        self.assertEqual(nvrs["nvr-01"]["overall_status"], "WARNING")                      # Unchanged by info.
        self.assertEqual(nvrs["nvr-04"]["channels"][0]["fleet_notes"],
                         ["Also recorded by nvr-01 ch1 (named “Silo” there)"])
        self.assertEqual(nvrs["nvr-01"]["channels"][1]["fleet_notes"], [])
        self.assertIn("ch.fleet_notes", DASHBOARD_UI)
        text = self.message(222, "/camera nvr-04 1")[0]["text"]
        self.assertIn("↔️ Also recorded by nvr-01 ch1 (named “Silo” there)", text)
        self.assertNotIn("192.0.2.1", text)


class EmailTests(unittest.TestCase):
    def test_fleet_run_adds_the_note_to_each_nvr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = []
            for nvr_id, channels in (("nvr-01", [cam(20, "198.51.100.149", "Silo")]),
                                     ("nvr-04", [cam(21, "198.51.100.149", "Silo")])):
                path = root / f"{nvr_id}_20261105T080000000000+0700.json"
                path.write_text(json.dumps({"nvr": {"id": nvr_id}, "overall": {"status": "HEALTHY"},
                                            "channel_inventory": {"channels": channels}}), encoding="utf-8")
                results.append(subprocess.CompletedProcess(["report"], 0, stdout=f"JSON report saved: {path}\n",
                                                           stderr=""))
            inventory = [SimpleNamespace(id="nvr-01", ip="198.51.100.1", http_port=80, name="Kiln"),
                         SimpleNamespace(id="nvr-04", ip="198.51.100.4", http_port=80, name="")]
            with patch.object(fleet, "REPORTS_DIR", root), patch.object(dashboard, "REPORTS_DIR", root), \
                    patch.object(fleet, "load_inventory", return_value=inventory), \
                    patch.object(fleet.subprocess, "run", side_effect=results), \
                    patch.object(fleet, "send_email_report", return_value=(True, "SENT")) as send, \
                    patch("sys.stdout"):
                fleet.main()
        rows = {row["id"]: row for row in send.call_args[0][0]}
        self.assertIn("Cameras also recorded by another NVR: ch20 = nvr-04 ch21. Fine if intended; "
                      "it uses disk space on both.", rows["nvr-01"]["findings"])
        self.assertTrue(any("ch21 = nvr-01 ch20" in f for f in rows["nvr-04"]["findings"]))


if __name__ == "__main__":
    unittest.main()
