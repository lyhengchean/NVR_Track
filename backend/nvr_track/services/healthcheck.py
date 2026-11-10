"""Docker healthchecks for the long-running services. Local only: never
contacts an NVR, Telegram or the mail server, and prints no settings.

  python -m nvr_track.services.healthcheck dashboard               # the web server answers
  python -m nvr_track.services.healthcheck heartbeat PATH SECONDS  # PATH was written in the last SECONDS

Exit 0 = healthy, 1 = unhealthy (Docker shows it in `docker compose ps`).
"""
from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def dashboard() -> tuple[bool, str]:
    """Any HTTP answer below 500 is healthy; 401 (login required) is the normal one."""
    port = os.environ.get("DASHBOARD_PORT", "8080").strip() or "8080"
    if not port.isdigit():
        return False, "DASHBOARD_PORT is not a number"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Never through a proxy.
    try:
        with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, OSError) as exc:
        return False, f"dashboard not answering ({type(exc).__name__})"
    return code < 500, f"dashboard HTTP {code}"


def heartbeat(path: str, seconds: str) -> tuple[bool, str]:
    try:
        limit = int(seconds)
    except ValueError:
        return False, "SECONDS is not a number"
    try:
        age = time.time() - Path(path).stat().st_mtime
    except OSError:
        return False, f"no heartbeat file {Path(path).name}"
    return age <= limit, f"heartbeat {max(age, 0):.0f} s old (limit {limit} s)"


def main(argv: list[str]) -> int:
    if argv[:1] == ["dashboard"] and len(argv) == 1:
        healthy, text = dashboard()
    elif argv[:1] == ["heartbeat"] and len(argv) == 3:
        healthy, text = heartbeat(argv[1], argv[2])
    else:
        print("usage: python -m nvr_track.services.healthcheck dashboard | heartbeat PATH SECONDS")
        return 2
    print(("healthy: " if healthy else "unhealthy: ") + text)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
