"""Unit tests for NVR inventory validation."""

import json
import tempfile
import unittest
from pathlib import Path

from app.config import InventoryError, load_inventory


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def load_test_data(self, data):
        path = Path(self.temp_dir.name) / "inventory.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return load_inventory(path)

    def test_example_inventory_has_12_nvrs_and_360_channels(self):
        path = PROJECT_ROOT / "config" / "nvrs.example.json"
        nvrs = load_inventory(path)

        self.assertEqual(len(nvrs), 12)
        self.assertEqual(sum(n.expected_channels for n in nvrs), 360)

    def test_rejects_duplicate_device_id(self):
        data = {
            "schema_version": 1,
            "nvrs": [
                {"id": "nvr-01", "ip": "192.0.2.1",
                 "expected_channels": 8},
                {"id": "nvr-01", "ip": "192.0.2.2",
                 "expected_channels": 8}
            ]
        }

        with self.assertRaisesRegex(InventoryError, "duplicate device ID"):
            self.load_test_data(data)

    def test_rejects_duplicate_ip(self):
        data = {
            "schema_version": 1,
            "nvrs": [
                {"id": "nvr-01", "ip": "192.0.2.1",
                 "expected_channels": 8},
                {"id": "nvr-02", "ip": "192.0.2.1",
                 "expected_channels": 8}
            ]
        }

        with self.assertRaisesRegex(InventoryError, "duplicate IP"):
            self.load_test_data(data)

    def test_rejects_invalid_ip(self):
        data = {
            "schema_version": 1,
            "nvrs": [
                {"id": "nvr-01", "ip": "not-an-ip",
                 "expected_channels": 8}
            ]
        }

        with self.assertRaisesRegex(InventoryError, "invalid IP"):
            self.load_test_data(data)

    def test_rejects_invalid_channel_count(self):
        data = {
            "schema_version": 1,
            "nvrs": [
                {"id": "nvr-01", "ip": "192.0.2.1",
                 "expected_channels": 0}
            ]
        }

        with self.assertRaisesRegex(
            InventoryError, "expected_channels"
        ):
            self.load_test_data(data)


if __name__ == "__main__":
    unittest.main()
