"""Project layout (Nov 2026): every module named by compose.yaml, the Dockerfile
and the server scripts exists, child processes find the package, and the
dashboard serves the frontend files. No network beyond 127.0.0.1."""
import base64
import importlib.util
import os
import re
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from nvr_track.core.paths import BACKEND_DIR, FRONTEND_DIR, PROJECT_ROOT, child_env
from nvr_track.web import dashboard

MODULE = re.compile(r"""(?:-m["',\s]+|from |import )["']?(nvr_track(?:\.[a-z_]+)+)""")
OLD_NAMES = re.compile(r"""-m["',\s]+["']?(?:app|scripts)\.|^\s*from (?:app|scripts)[ .]|import (?:app|scripts)\b""", re.M)


def present(*names):
    return [PROJECT_ROOT / n for n in names if (PROJECT_ROOT / n).exists()]


class LayoutTests(unittest.TestCase):
    def test_folders(self):
        self.assertTrue((BACKEND_DIR / "nvr_track" / "__init__.py").is_file())
        self.assertEqual(BACKEND_DIR.parent, PROJECT_ROOT)
        for page in ("dashboard", "report-builder"):
            for name in ("index.html", f"{page}.js", f"{page}.css"):
                self.assertTrue((FRONTEND_DIR / page / name).is_file(), f"{page}/{name}")

    def test_every_module_named_by_deploy_files_exists(self):
        files = present("compose.yaml", "deploy/Dockerfile", "scripts/deploy_check.sh", "scripts/fill_calendars.sh")
        if not files:
            self.skipTest("deploy files are not part of this test image")
        named = set()
        for path in files:
            text = path.read_text(encoding="utf-8")
            named |= set(MODULE.findall(text))
            self.assertIsNone(OLD_NAMES.search(text), f"{path.name} still names app.* or scripts.*")
        self.assertIn("nvr_track.web.dashboard", named)
        self.assertIn("nvr_track.services.healthcheck", named)
        for name in sorted(named):
            self.assertIsNotNone(importlib.util.find_spec(name), name)

    def test_compose_builds_from_deploy_dockerfile(self):
        files = present("compose.yaml")
        if not files:
            self.skipTest("compose.yaml is not part of this test image")
        text = files[0].read_text(encoding="utf-8")
        self.assertEqual(text.count("dockerfile: deploy/Dockerfile"), text.count("    build:"))

    def test_child_processes_find_the_package_without_pythonpath(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        with patch.dict(os.environ, env, clear=True):
            child = child_env()
        self.assertEqual(child["PYTHONPATH"].split(os.pathsep)[0], str(BACKEND_DIR))
        result = subprocess.run([sys.executable, "-c", "import nvr_track.cli.run_nvr_report"],
                                cwd=PROJECT_ROOT, env=child, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])


class StaticFileTests(unittest.TestCase):
    def setUp(self):
        for target, value in (("DASHBOARD_USERNAME", "demo"), ("DASHBOARD_PASSWORD", "test-password-long"),
                              ("LOGIN_GUARD", dashboard.LoginGuard())):
            patcher = patch.object(dashboard, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.auth = "Basic " + base64.b64encode(b"demo:test-password-long").decode()

    def get(self, path, auth=True):
        try:
            with urlopen(Request(self.base + path, headers={"Authorization": self.auth} if auth else {}),
                         timeout=5) as response:
                return response.status, response.headers.get("Content-Type"), response.read()
        except HTTPError as error:
            return error.code, error.headers.get("Content-Type"), b""

    def test_pages_load_their_files_from_this_server(self):
        for path, page in (("/", "dashboard"), ("/reports", "report-builder")):
            html = self.get(path)[2].decode("utf-8")
            for ext, kind in ((".js", "text/javascript"), (".css", "text/css")):
                url = f"/static/{page}/{page}{ext}"
                self.assertIn(url, html)
                status, content_type, body = self.get(url)
                self.assertEqual(status, 200, url)
                self.assertTrue(content_type.startswith(kind), content_type)
                self.assertEqual(body, (FRONTEND_DIR / page / f"{page}{ext}").read_bytes())
                self.assertEqual(self.get(url, auth=False)[0], 401)     # Behind the login too.

    def test_only_the_listed_files_are_served(self):
        for path in ("/static/dashboard/index.html", "/static/", "/static/dashboard/../../compose.yaml",
                     "/static/%2e%2e/backend/nvr_track/core/paths.py", "/frontend/dashboard/dashboard.js"):
            self.assertEqual(self.get(path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
