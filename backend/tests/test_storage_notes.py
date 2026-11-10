"""Full disks that are being overwritten, and bays confirmed empty on site."""
import json
import tempfile
import unittest
from pathlib import Path

from nvr_track.core.config import InventoryError, load_inventory
from nvr_track.nvr.storage import HDDEntry, StorageCheck, classify_storage
from nvr_track.cli.run_nvr_report import storage_after_recording


def disks(*states):
    return tuple(
        HDDEntry(id=disk_id, name="hdd", disk_type="SATA", status=status,
                 capacity_mb=0 if status == "notexist" else 3815447,
                 free_space_mb=free, property="RW", group="1")
        for disk_id, status, free in states
    )


class FullDiskTests(unittest.TestCase):
    def test_full_disk_without_recording_evidence_stays_warning(self):
        status, message = classify_storage(disks(("1", "ok", 0)))
        self.assertEqual(status, "WARNING")
        self.assertIn("verify overwrite", message)

    def test_full_disk_with_recent_footage_is_healthy_with_note(self):
        status, message = classify_storage(disks(("1", "ok", 0), ("2", "ok", 0)), recent_write_minutes=60)
        self.assertEqual(status, "HEALTHY")
        self.assertIn("HDDs 1, 2 are full", message)
        self.assertIn("HDD 1 is full", classify_storage(disks(("1", "ok", 0)), recent_write_minutes=60)[1])
        self.assertIn("overwritten", message)

    def test_disk_error_is_never_relaxed(self):
        status, _ = classify_storage(disks(("1", "ok", 0), ("3", "error", 0)), recent_write_minutes=60)
        self.assertEqual(status, "CRITICAL")

    def test_missing_expected_disk_is_never_relaxed(self):
        status, _ = classify_storage(disks(("1", "ok", 0), ("2", "notexist", 0)),
                                     expected_sata_hdd_ids=(1, 2), recent_write_minutes=60)
        self.assertEqual(status, "CRITICAL")


class EmptyBayTests(unittest.TestCase):
    def test_confirmed_empty_bays_are_noted_not_warned(self):
        status, message = classify_storage(
            disks(("1", "ok", 100), ("3", "notexist", 0), ("5", "notexist", 0)),
            empty_sata_hdd_ids=(3, 5))
        self.assertEqual(status, "HEALTHY")
        self.assertIn("bays 3, 5 are empty", message)

    def test_unconfirmed_bay_still_warns(self):
        status, message = classify_storage(
            disks(("1", "ok", 100), ("3", "notexist", 0), ("7", "notexist", 0)),
            empty_sata_hdd_ids=(3,))
        self.assertEqual(status, "WARNING")
        self.assertIn("not detected (7)", message)

    def test_fleet_pattern_nvr_03_with_both_settings(self):
        status, _ = classify_storage(
            disks(("1", "ok", 0), ("2", "ok", 0), ("3", "notexist", 0),
                  ("5", "notexist", 0), ("6", "ok", 0), ("7", "notexist", 0)),
            empty_sata_hdd_ids=(3, 5, 7), recent_write_minutes=60)
        self.assertEqual(status, "HEALTHY")


class InventoryTests(unittest.TestCase):
    def load(self, **extra):
        entry = {"id": "nvr-01", "ip": "192.0.2.1", "expected_channels": 4, **extra}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nvrs.json"
            path.write_text(json.dumps({"schema_version": 1, "nvrs": [entry]}), encoding="utf-8")
            return load_inventory(path)[0]

    def test_empty_bays_are_loaded(self):
        self.assertEqual(self.load(empty_sata_hdd_ids=[3, 5]).empty_sata_hdd_ids, (3, 5))
        self.assertEqual(self.load().empty_sata_hdd_ids, ())

    def test_bay_cannot_be_both_expected_and_empty(self):
        with self.assertRaises(InventoryError):
            self.load(expected_sata_hdd_ids=[1, 3], empty_sata_hdd_ids=[3])

    def test_invalid_empty_bay_values_are_rejected(self):
        for bad in ([0], [True], ["3"], [3, 3], "3"):
            with self.subTest(bad=bad), self.assertRaises(InventoryError):
                self.load(empty_sata_hdd_ids=bad)


class ReporterTests(unittest.TestCase):
    class Nvr:
        expected_sata_hdd_ids = ()
        empty_sata_hdd_ids = ()

    def storage(self):
        return StorageCheck("WARNING", 200, disks(("1", "ok", 0)), "Zero free space")

    def test_reclassified_when_cameras_recorded(self):
        result = storage_after_recording(self.storage(), self.Nvr, {"enabled": True, "evidence": 5}, 60)
        self.assertEqual(result.status, "HEALTHY")
        self.assertEqual(len(result.disks), 1)

    def test_unchanged_without_evidence_or_checks(self):
        for summary in ({"enabled": True, "evidence": 0}, {"enabled": False, "evidence": 9}):
            with self.subTest(summary=summary):
                self.assertEqual(storage_after_recording(self.storage(), self.Nvr, summary, 60).status, "WARNING")

    def test_unreachable_storage_is_untouched(self):
        unreachable = StorageCheck("UNKNOWN", None, (), "Network request failed or timed out.")
        self.assertIs(storage_after_recording(unreachable, self.Nvr, {"enabled": True, "evidence": 5}, 60), unreachable)


if __name__ == "__main__":
    unittest.main()
