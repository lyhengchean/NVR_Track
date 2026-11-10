"""Move old per-NVR JSON reports into compressed monthly archives.

Reports are never deleted outright: each selected file is first written into
reports/archive/reports-YYYY-MM-<run>.tar.gz, the archive is re-read and
checked, and only then are the originals removed. Without --apply nothing
changes (dry run). Contacts no NVR.

Kept in place always: the newest --keep-per-nvr reports of every NVR (the
reporter reuses calendars and history from recent reports), every report
newer than --older-than-days, and every non-report file (fleet_latest_run.json,
track-verification-*.json, locks).

  docker compose run --rm -T --entrypoint python nvr-report -m nvr_track.cli.archive_reports
  docker compose run --rm -T --entrypoint python nvr-report -m nvr_track.cli.archive_reports --apply
"""
from __future__ import annotations

import argparse
import os
import re
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nvr_track.core.paths import PROJECT_ROOT as ROOT

REPORTS_DIR = ROOT / "reports"
# Same naming rule as run_nvr_report / nvr_track.web.dashboard.REPORT_NAME.
REPORT_NAME = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9_-]{0,63}?)_"
                         r"(?P<stamp>\d{8}T\d{6}(?:\d{1,6})?[+-]\d{4})\.json$")
MIN_KEEP_PER_NVR = 30


def _stamp(text: str) -> datetime | None:
    for pattern in ("%Y%m%dT%H%M%S%f%z", "%Y%m%dT%H%M%S%z"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def select(report_dir: Path, older_than_days: int, keep_per_nvr: int,
           now: datetime | None = None) -> dict[str, list[Path]]:
    """{'YYYY-MM': [paths]} of reports that may be archived."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=older_than_days)
    by_nvr: dict[str, list[tuple[datetime, Path]]] = {}
    for path in report_dir.glob("*_*.json"):
        match = REPORT_NAME.match(path.name)
        moment = _stamp(match["stamp"]) if match else None
        if moment is None or not path.is_file() or path.is_symlink():
            continue
        by_nvr.setdefault(match["id"], []).append((moment, path))
    months: dict[str, list[Path]] = {}
    for items in by_nvr.values():
        items.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        for moment, path in items[keep_per_nvr:]:
            if moment < cutoff:
                months.setdefault(moment.strftime("%Y-%m"), []).append(path)
    return {month: sorted(paths) for month, paths in sorted(months.items())}


def archive_month(report_dir: Path, month: str, paths: list[Path], run: str) -> Path:
    """Write, verify, then remove. Raises (and keeps every original) on any problem."""
    target_dir = report_dir / "archive"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"reports-{month}-{run}.tar.gz"
    temporary = target_dir / f".{target.name}.tmp"
    with tarfile.open(temporary, "w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=path.name, recursive=False)
    with tarfile.open(temporary, "r:gz") as archive:
        stored = {member.name: member.size for member in archive.getmembers()}
    for path in paths:
        if stored.get(path.name) != path.stat().st_size:
            temporary.unlink(missing_ok=True)
            raise OSError(f"Archive check failed for {path.name}; nothing removed.")
    os.replace(temporary, target)
    for path in paths:
        path.unlink()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive old NVR reports (dry run unless --apply).")
    parser.add_argument("--older-than-days", type=int,
                        default=int(os.environ.get("NVR_REPORT_ARCHIVE_DAYS", "0") or 0) or 60)
    parser.add_argument("--keep-per-nvr", type=int, default=200)
    parser.add_argument("--apply", action="store_true", help="Really archive (default: only list).")
    args = parser.parse_args(argv)
    if not 7 <= args.older_than_days <= 3650:
        print("--older-than-days must be 7-3650.")
        return 2
    keep = max(args.keep_per_nvr, MIN_KEEP_PER_NVR)
    months = select(REPORTS_DIR, args.older_than_days, keep)
    total = sum(len(paths) for paths in months.values())
    size = sum(path.stat().st_size for paths in months.values() for path in paths)
    print(f"Reports older than {args.older_than_days} days (keeping the newest {keep} per NVR): "
          f"{total} files, {size / 1e6:.1f} MB")
    for month, paths in months.items():
        print(f"  {month}: {len(paths)} files")
    if not total:
        return 0
    if not args.apply:
        print("Dry run: nothing changed. Add --apply to archive them.")
        return 0
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for month, paths in months.items():
        try:
            target = archive_month(REPORTS_DIR, month, paths, run)
        except (OSError, tarfile.TarError) as exc:
            print(f"  {month}: NOT archived ({type(exc).__name__}); originals kept.")
            return 1
        print(f"  {month}: archived to {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
