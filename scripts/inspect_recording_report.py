"""Inspect saved JSON evidence for selected channels; makes no ISAPI requests."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent


def latest_report(nvr_id: str, report_dir: Path) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", nvr_id):
        raise ValueError("Invalid NVR identifier")
    for path in sorted(report_dir.glob(f"{nvr_id}_*.json"), reverse=True):
        try:
            with path.open("r", encoding="utf-8") as stream:
                report = json.load(stream)
            if report.get("nvr", {}).get("id") == nvr_id:
                return report
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    raise ValueError("No readable saved report for the requested NVR")


def window_overlap(result: dict) -> bool:
    """Confirm saved recording segment overlaps its saved search interval."""
    try:
        segment_start = datetime.fromisoformat(result["most_recent_segment_start"])
        segment_end = datetime.fromisoformat(result["most_recent_segment_end"])
        window = result["requested_window_utc"]
        start = datetime.fromisoformat(window["start"])
        end = datetime.fromisoformat(window["end"])
        return (all(d.tzinfo is not None for d in (segment_start, segment_end, start, end))
                and segment_start < segment_end and start < end
                and segment_start <= end and segment_end >= start)
    except (ValueError, TypeError, KeyError):
        return False


def channel_findings(report: dict, channels: set[str]) -> list[dict]:
    indexed = {
        str(ch["channel_id"]): ch
        for ch in report.get("channel_inventory", {}).get("channels", [])
        if isinstance(ch, dict) and "channel_id" in ch
    }
    results = []
    for channel_id in sorted(channels, key=lambda text: (not text.isdecimal(), int(text) if text.isdecimal() else text)):
        row = indexed.get(channel_id, {})
        recent = row.get("recording_result") or {}
        old = row.get("historical_recording_result") or {}
        results.append({
            "channel": channel_id,
            "live": row.get("reported_status", "NOT_RETURNED"),
            "expected": row.get("recording_expectation", "unspecified"),
            "track_id": recent.get("track_id"),
            "track_verified": recent.get("track_verified") is True,
            "recent": recent.get("status", "NOT_CHECKED"),
            "recent_overlap": window_overlap(recent),
            "recent_segment_end": recent.get("most_recent_segment_end"),
            "recent_checked": recent.get("checked_at"),
            "recent_message": recent.get("message", "No check available"),
            "historical": old.get("status", "HISTORICAL_NOT_CHECKED"),
            "historical_segment_end": old.get("most_recent_segment_end"),
            "historical_complete": old.get("search_complete") is True,
        })
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nvr-id", default="nvr-11")
    parser.add_argument("--channels", default="12,13,14,15,16")
    args = parser.parse_args()
    try:
        selected = {part.strip() for part in args.channels.split(",") if part.strip()}
        if not selected or any(not part.isdecimal() for part in selected):
            raise ValueError("Channel IDs must be positive integers")
        report = latest_report(args.nvr_id, ROOT / "reports")
    except ValueError as exc:
        print(f"Cannot inspect report: {exc}")
        return 2
    print("Saved report completed:", report.get("check_completed_at", "NOT_RETURNED"))
    print("Approximate NVR clock offset (seconds):",
          report.get("per_camera_recording", {}).get("nvr_clock_offset_seconds_approx", "NOT_AVAILABLE"))
    good = True
    for row in channel_findings(report, selected):
        print("\nChannel:", row["channel"])
        for field in ("live", "expected", "track_id", "track_verified", "recent",
                      "recent_overlap", "recent_segment_end", "recent_checked",
                      "recent_message", "historical", "historical_segment_end", "historical_complete"):
            print(f"  {field}: {row[field]}")
        if (row["live"] != "ONLINE" or not row["track_verified"]
                or row["recent"] not in {"RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY"}
                or not row["recent_overlap"]):
            good = False
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
