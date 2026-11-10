"""Where NVR_Track keeps its files.

PROJECT_ROOT is the repository folder: /opt/NVR_Track on the server and /app in
the containers. config/, reports/ and state/ live there; the Python package is
in backend/ and the web pages in frontend/.
"""
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]    # backend/nvr_track/core/paths.py
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"


def child_env() -> dict[str, str]:
    """Environment for `python -m nvr_track...` child processes (run from
    PROJECT_ROOT): the package stays importable however the parent was started."""
    paths = [str(BACKEND_DIR)] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(dict.fromkeys(paths))}
