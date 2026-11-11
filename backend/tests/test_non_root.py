"""Containers run as UID/GID 10001: the image, the compose file, and
scripts/container_files.sh that gives that user its files on the server
(run with a logging stand-in for chown, so no root is needed)."""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from nvr_track.core.paths import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "container_files.sh"
DOCKERFILE = PROJECT_ROOT / "deploy" / "Dockerfile"


class ImageTests(unittest.TestCase):
    def test_test_and_runtime_stages_end_as_user_10001(self):
        if not DOCKERFILE.exists():
            self.skipTest("deploy/Dockerfile is not part of this test image")
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("useradd --uid 10001 --gid 10001", text)
        stages = re.split(r"(?m)^FROM ", text)[1:]
        names = [stage.split("\n", 1)[0].split(" AS ")[-1] for stage in stages]
        self.assertEqual(names, ["base", "test", "runtime"])
        for name, stage in zip(names[1:], stages[1:]):
            users = re.findall(r"(?m)^USER (.+)$", stage)
            self.assertEqual(users[-1:], ["10001:10001"], name)
            self.assertLess(stage.index("USER 10001:10001"), stage.index("ENTRYPOINT"), name)

    def test_the_test_image_runs_as_10001(self):
        if not os.environ.get("NVR_TRACK_TEST_IMAGE"):
            self.skipTest("only inside the Docker test image")
        self.assertEqual((os.getuid(), os.getgid()), (10001, 10001))


@unittest.skipUnless(SCRIPT.exists() and shutil.which("bash") and shutil.which("find"),
                     "scripts/container_files.sh needs bash and find")
class ContainerFilesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = Path(temp.name) / "app"
        self.outside = Path(temp.name) / "outside.txt"
        self.outside.write_text("not ours")
        for folder in ("reports/archive", "state/notes", "state/events", "config/tls"):
            (self.app / folder).mkdir(parents=True)
        (self.app / "reports" / "nvr-01_1.json").write_text("{}")
        (self.app / "reports" / "archive" / "old.tar.gz").write_bytes(b"x")
        (self.app / "state" / "notes" / "notes.json").write_text("{}")
        (self.app / "state" / "link").symlink_to(self.outside)
        for name in ("config/nvrs.local.json", "config/tls/dashboard.key", ".env"):
            (self.app / name).write_text("secret-free placeholder")
            (self.app / name).chmod(0o600)
        self.stubs = Path(temp.name) / "bin"
        self.stubs.mkdir()
        self.log = Path(temp.name) / "chown.log"

    def run_script(self, chown_exit=0):
        stub = self.stubs / "chown"
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{self.log}"\nexit {chown_exit}\n')
        stub.chmod(0o755)
        env = {**os.environ, "APP_DIR": str(self.app), "PATH": f"{self.stubs}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)

    def chown_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def not_ours(self):
        """Entries under reports/ and state/ not owned by 10001:10001 (all of them unless run as 10001)."""
        found = set()
        for top in ("reports", "state"):
            for path in [self.app / top, *(self.app / top).rglob("*")]:
                info = path.lstat()
                if (info.st_uid, info.st_gid) != (10001, 10001):
                    found.add(str(path.relative_to(self.app)))
        return found

    def test_gives_reports_state_and_read_access_without_following_links(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for folder in ("scan", "notes", "events", "snapshots", "telegram", "live", "custom_reports"):
            self.assertTrue((self.app / "state" / folder).is_dir(), folder)
        expected = self.not_ours()
        changed = set()
        for call in self.chown_calls():
            if call.startswith("-h 10001:10001 "):
                changed.update(call.split()[2:])
        self.assertEqual(changed, expected)
        self.assertEqual(result.stdout.strip(), str(len(expected)))
        self.assertNotIn(str(self.outside), " ".join(self.chown_calls()))     # Links are not followed.
        for name in ("config/nvrs.local.json", "config/tls/dashboard.key"):
            self.assertIn(f"root:10001 {name}", self.chown_calls())
            self.assertEqual(stat.S_IMODE((self.app / name).stat().st_mode), 0o640, name)
        self.assertIn("root:10001 config/tls", self.chown_calls())
        self.assertEqual(stat.S_IMODE((self.app / "config" / "tls").stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE((self.app / ".env").stat().st_mode), 0o600)      # .env untouched.
        self.assertNotIn(".env", " ".join(self.chown_calls()))
        self.assertEqual(self.outside.read_text(), "not ours")

    def test_a_failed_chown_is_reported(self):
        result = self.run_script(chown_exit=1)
        self.assertEqual(result.returncode, 1)

    def test_deploy_check_runs_it_before_starting_containers(self):
        deploy = (PROJECT_ROOT / "scripts" / "deploy_check.sh").read_text(encoding="utf-8")
        self.assertLess(deploy.index("bash scripts/container_files.sh"),
                        deploy.index("docker compose up -d --force-recreate nvr-dashboard nvr-scanner"))
        self.assertIn("docker inspect -f '{{.Config.User}}'", deploy)
        self.assertIn('[ "$FPERM" = "640 root:10001" ]', deploy)


if __name__ == "__main__":
    unittest.main()
