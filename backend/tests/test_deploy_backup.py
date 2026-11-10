"""scripts/deploy_check.sh section 1: one backup, replaced by each deploy;
older backups removed once the new one is safe; a restarted run keeps the
backup taken before the pull. Runs the real shell code in a temp folder."""
import gzip
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from nvr_track.core.paths import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "deploy_check.sh"
STUBS = """set -u
PASS=0; WARN=0; FAIL=0
pass() { echo "PASS $*"; }
warn() { echo "WARN $*"; }
fail() { echo "FAIL $*"; }
section() { :; }
STAMP=20261110-101207
"""


def section_one() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    return text.split('section "1. Backup"', 1)[1].split('section "2. Source code"', 1)[0]


def old_backup(path: Path, text: str, age: int) -> Path:
    with gzip.open(path, "wb") as handle:
        handle.write(text.encode())
    os.utime(path, (time.time() - age, time.time() - age))
    return path


@unittest.skipUnless(SCRIPT.exists() and shutil.which("bash") and shutil.which("tar"), "needs bash, tar and the script")
class BackupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.app, self.backups = root / "app", root / "backups"
        (self.app / "reports").mkdir(parents=True)
        (self.app / "state").mkdir()
        self.backups.mkdir()
        (self.app / ".env").write_text("NVR_PASSWORD=not-a-real-one\n")
        (self.app / "state" / "notes.json").write_text("{}")
        (self.app / "reports" / "nvr-01_big.json").write_text("{}")

    def run_section(self, restarted=False, app=None):
        env = {"PATH": os.environ["PATH"], "APP_DIR": str(app or self.app), "NVR_BACKUP_DIR": str(self.backups)}
        if restarted:
            env["NVR_DEPLOY_CHECK_LOG"] = "/dev/null"
        script = STUBS + f'APP_DIR="{env["APP_DIR"]}"\n' + section_one() + "\necho DONE\n"
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout

    def names(self):
        return sorted(p.name for p in self.backups.iterdir())

    def test_one_backup_replaces_all_older_ones(self):
        for n in range(3):
            old_backup(self.backups / f"nvr-track-backup-2026110{n}-084342.tgz", "old", age=3600 * (n + 1))
        (self.backups / "keep-me.tgz").write_text("not ours")
        code, out = self.run_section()
        self.assertEqual(code, 0, out)
        self.assertIn("PASS Backup saved to", out)
        self.assertEqual(self.names(), ["keep-me.tgz", "nvr-track-backup.tgz"])
        backup = self.backups / "nvr-track-backup.tgz"
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        with tarfile.open(backup) as tar:
            members = tar.getnames()
        self.assertIn("./.env", members)
        self.assertNotIn("./reports/nvr-01_big.json", members)      # Reports are not backed up.

    def test_next_deploy_overwrites_it(self):
        self.run_section()
        (self.app / "state" / "notes.json").write_text('{"changed": true}')
        code, out = self.run_section()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.names(), ["nvr-track-backup.tgz"])
        with tarfile.open(self.backups / "nvr-track-backup.tgz") as tar:
            self.assertIn(b"changed", tar.extractfile("./state/notes.json").read())

    def test_restart_keeps_the_backup_from_before_the_pull(self):
        """The old script (dated names) backs up and pulls; the new one restarts."""
        old_backup(self.backups / "nvr-track-backup-20261101-080000.tgz", "week old", age=9 * 86400)
        before_pull = old_backup(self.backups / "nvr-track-backup-20261110-101207.tgz", "before pull", age=5)
        content = before_pull.read_bytes()
        code, out = self.run_section(restarted=True)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS Backup from before the pull kept", out)
        self.assertEqual(self.names(), ["nvr-track-backup.tgz"])
        self.assertEqual((self.backups / "nvr-track-backup.tgz").read_bytes(), content)   # No second backup.

    def test_a_failed_backup_keeps_the_previous_one_and_stops(self):
        old_backup(self.backups / "nvr-track-backup.tgz", "previous", age=60)
        code, out = self.run_section(app=self.app / "missing")
        self.assertEqual(code, 1)
        self.assertIn("FAIL Backup failed (previous backup kept)", out)
        self.assertNotIn("DONE", out)
        self.assertEqual(self.names(), ["nvr-track-backup.tgz"])      # No temp file left behind.

    def test_restart_with_a_broken_backup_makes_a_new_one(self):
        (self.backups / "nvr-track-backup.tgz").write_bytes(b"not gzip")
        code, out = self.run_section(restarted=True)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS Backup saved to", out)
        with tarfile.open(self.backups / "nvr-track-backup.tgz") as tar:
            self.assertIn("./.env", tar.getnames())


if __name__ == "__main__":
    unittest.main()
