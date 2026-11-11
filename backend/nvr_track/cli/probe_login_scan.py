"""Child process of `probe_nvr_features --scan/--ask-login`; not for direct use.

Runs the normal single-NVR scan (run_nvr_report) unchanged and also writes
every final NVR answer 401 or 403 to the file named by NVR_PROBE_REFUSALS,
one JSON line [method, path, status]. Only the URL path is written: no query
string, headers or credentials.
"""
from __future__ import annotations

import json
import os
import threading
from urllib.parse import urlsplit

import requests

from nvr_track.cli import run_nvr_report


def record_refusals(log_path: str) -> None:
    """Log the final answer of every request (after Digest login) that was 401/403."""
    original = requests.Session.send
    lock = threading.Lock()

    def send(self, request, **kwargs):
        response = original(self, request, **kwargs)
        if response.status_code in (401, 403):
            line = json.dumps([request.method, urlsplit(request.url).path, response.status_code])
            with lock, open(log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return response

    requests.Session.send = send


def main() -> int:
    record_refusals(os.environ["NVR_PROBE_REFUSALS"])
    return run_nvr_report.main()


if __name__ == "__main__":
    raise SystemExit(main())
