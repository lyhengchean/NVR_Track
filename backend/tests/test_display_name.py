"""An optional display name labels an NVR without changing its technical id."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nvr_track.web import dashboard
from nvr_track.core.config import InventoryError, load_inventory
from nvr_track.cli.run_all_nvr_reports import send_email_report
from frontend_source import DASHBOARD_UI


def inventory(tmp: Path, **extra) -> Path:
    path = tmp / "nvrs.json"
    entry = {"id": "nvr-01", "ip": "192.0.2.51", "expected_channels": 2, **extra}
    path.write_text(json.dumps({"schema_version": 1, "nvrs": [entry]}), encoding="utf-8")
    return path


class DisplayNameTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)

    def test_name_is_optional_and_trimmed(self):
        self.assertEqual(load_inventory(inventory(self.tmp))[0].name, "")
        nvr = load_inventory(inventory(self.tmp, name="  Kiln Building  "))[0]
        self.assertEqual((nvr.id, nvr.name), ("nvr-01", "Kiln Building"))

    def test_bad_names_are_rejected(self):
        for bad in (123, "x" * 81, "Line\nbreak"):
            with self.assertRaises(InventoryError):
                load_inventory(inventory(self.tmp, name=bad))

    def test_dashboard_exposes_name_and_old_reports_have_none(self):
        for nvr_id, extra in (("nvr-01", {"name": "Kiln <b>Building</b>"}), ("nvr-02", {})):
            report = {"schema_version": 2, "nvr": {"id": nvr_id, "ip": "192.0.2.1", **extra},
                      "check_completed_at": "2026-11-02T08:00:00+07:00", "overall": {"status": "WARNING"},
                      "channel_inventory": {"channels": []}, "storage": {"status": "OK", "disks": []}}
            (self.tmp / f"{nvr_id}_20261102T080000+0700.json").write_text(json.dumps(report), encoding="utf-8")
        with patch.object(dashboard, "REPORTS_DIR", self.tmp):
            nvrs = {n["id"]: n for n in dashboard.read_dashboard_data()["nvrs"]}
        self.assertEqual(nvrs["nvr-01"]["name"], "Kiln <b>Building</b>")
        self.assertEqual(nvrs["nvr-02"]["name"], "")
        self.assertIn('function nvrTitle(nvr) { return nvr.name || nvr.id; }', DASHBOARD_UI)
        self.assertIn('el("h3", "", nvrTitle(nvr))', DASHBOARD_UI)

    @patch("nvr_track.cli.run_all_nvr_reports.smtplib.SMTP")
    def test_email_shows_id_and_escaped_name(self, smtp):
        env = {"SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p",
               "SMTP_FROM": "a@example.com", "SMTP_TO": "b@example.com",
               "SMTP_SECURITY": "starttls", "SMTP_PORT": "587"}
        row = {"id": "nvr-01", "name": "Kiln <b>", "ip": "192.0.2.1", "overall": "WARNING",
               "storage": "OK", "recording": "X", "online": 1, "offline": 0}
        with patch.dict("os.environ", env):
            send_email_report([row])
        message = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        html = message.get_body(("html",)).get_content()
        self.assertIn("nvr-01 - Kiln &lt;b&gt;", html)


if __name__ == "__main__":
    unittest.main()
