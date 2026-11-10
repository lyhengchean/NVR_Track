"""Linux advisory per-NVR report lock to prevent overlapping recording searches."""

import os
from pathlib import Path


def acquire_report_lock(report_dir: Path, nvr_id: str):
    """Return a held lock file (caller closes), or None if another run owns it.

    The lock lives on the shared Compose reports bind mount so separate
    reporting containers can see the same lock. Linux production only.
    """
    try:
        import fcntl
    except ImportError:  # Windows local development without flock
        return None
    lock_dir = report_dir / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    # NVR IDs come from validated inventory (no slash allowed).
    descriptor = os.open(lock_dir / (nvr_id + ".lock"),
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(descriptor, "r+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    return handle
