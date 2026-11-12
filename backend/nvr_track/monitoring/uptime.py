"""Online and recording percentages per camera, from saved reports only.

Every saved report is one sample. Online % counts samples where the NVR
reported the camera ONLINE (of samples with a known live state); recording %
counts samples with verified recording evidence (of samples whose recording
check completed). Failed or disabled checks are left out, never counted as
recording or as not recording. Flaps are ONLINE/OFFLINE changes between
consecutive samples. No NVR is contacted.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from nvr_track.core.spreadsheet import csv_safe

REPORT_NAME = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9_-]{0,63}?)_"
                         r"(?P<stamp>\d{8}T\d{6}(?:\d{1,6})?[+-]\d{4})\.json$")
EVIDENCE = {"RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY"}
CHECKED = EVIDENCE | {"NO_RECENT_RECORDING_EVIDENCE"}


def _stamp(text: str) -> datetime | None:
    for pattern in ("%Y%m%dT%H%M%S%f%z", "%Y%m%dT%H%M%S%z"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def compute(report_dir: Path, start: datetime, end: datetime) -> dict:
    """Uptime between start (inclusive) and end (exclusive)."""
    files: dict[str, list[tuple[datetime, Path]]] = {}
    for path in report_dir.glob("*_*.json"):
        match = REPORT_NAME.match(path.name)
        moment = _stamp(match["stamp"]) if match else None
        if moment is not None and start <= moment < end:
            files.setdefault(match["id"], []).append((moment, path))
    nvrs, cameras = [], []
    for nvr_id in sorted(files):
        reports = reachable = restarts = 0
        name = ""
        cams: dict[str, dict] = {}
        for moment, path in sorted(files[nvr_id]):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    report = json.load(handle)
            except (OSError, ValueError):
                continue
            if not isinstance(report, dict) or (report.get("nvr") or {}).get("id") != nvr_id:
                continue
            reports += 1
            name = str((report.get("nvr") or {}).get("name") or name)
            reachable += (report.get("device_info") or {}).get("status") == "DEVICE_INFO_RETRIEVED"
            restarts += (report.get("restart") or {}).get("detected") is True
            for ch in (report.get("channel_inventory") or {}).get("channels") or []:
                if not isinstance(ch, dict):
                    continue
                cid = str(ch.get("channel_id", ""))
                cam = cams.setdefault(cid, {"nvr": nvr_id, "nvr_name": name, "channel": cid, "name": "",
                                            "samples": 0, "live_known": 0, "online": 0,
                                            "checked": 0, "recording": 0, "flaps": 0, "_last": None})
                cam["name"] = str(ch.get("name") or cam["name"])
                cam["samples"] += 1
                live = ch.get("reported_status")
                if live in {"ONLINE", "OFFLINE"}:
                    cam["live_known"] += 1
                    cam["online"] += live == "ONLINE"
                    if cam["_last"] is not None and cam["_last"] != live:
                        cam["flaps"] += 1
                    cam["_last"] = live
                status = (ch.get("recording_result") or {}).get("status")
                if status in CHECKED:
                    cam["checked"] += 1
                    cam["recording"] += status in EVIDENCE
        rows = []
        for cam in cams.values():
            cam.pop("_last")
            cam["online_pct"] = _pct(cam["online"], cam["live_known"])
            cam["recording_pct"] = _pct(cam["recording"], cam["checked"])
            rows.append(cam)
        rows.sort(key=lambda c: (int(c["channel"]) if c["channel"].isdigit() else 10**6, c["channel"]))
        cameras += rows
        checked = sum(c["checked"] for c in rows)
        live = sum(c["live_known"] for c in rows)
        nvrs.append({"nvr": nvr_id, "name": name, "reports": reports, "restarts": restarts,
                     "reachable_pct": _pct(reachable, reports), "cameras": len(rows),
                     "online_pct": _pct(sum(c["online"] for c in rows), live),
                     "recording_pct": _pct(sum(c["recording"] for c in rows), checked)})
    checked = sum(c["checked"] for c in cameras)
    live = sum(c["live_known"] for c in cameras)
    return {"start": start.isoformat(), "end": end.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "fleet": {"nvrs": len(nvrs), "cameras": len(cameras),
                      "online_pct": _pct(sum(c["online"] for c in cameras), live),
                      "recording_pct": _pct(sum(c["recording"] for c in cameras), checked)},
            "nvrs": nvrs, "cameras": cameras}


def to_csv(result: dict) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["NVR", "NVR name", "Channel", "Camera", "Samples", "Online %", "Recording %",
                     "Recording checks", "Online/offline changes"])
    for c in result["cameras"]:
        row = [c["nvr"], c["nvr_name"], c["channel"], c["name"], c["samples"],
               "" if c["online_pct"] is None else c["online_pct"],
               "" if c["recording_pct"] is None else c["recording_pct"], c["checked"], c["flaps"]]
        writer.writerow([csv_safe(v) if isinstance(v, str) else v for v in row])
    return out.getvalue()


def worst(result: dict, key: str, limit: int = 10) -> list[dict]:
    rows = [c for c in result["cameras"] if c[key] is not None and c[key] < 100]
    return sorted(rows, key=lambda c: (c[key], c["nvr"], c["channel"]))[:limit]


def flapping(result: dict, minimum: int = 3, limit: int = 10) -> list[dict]:
    rows = [c for c in result["cameras"] if c["flaps"] >= minimum]
    return sorted(rows, key=lambda c: -c["flaps"])[:limit]


def summary_lines(result: dict, title: str) -> list[str]:
    fleet = result["fleet"]

    def pct(v):
        return "—" if v is None else f"{v:.1f}%"
    lines = [title,
             f"Cameras {fleet['cameras']} on {fleet['nvrs']} NVRs · online {pct(fleet['online_pct'])}"
             f" · recording {pct(fleet['recording_pct'])}", ""]
    lines += [f"{n['nvr']}{' ' + n['name'] if n['name'] else ''}: online {pct(n['online_pct'])}, "
              f"recording {pct(n['recording_pct'])}, NVR reachable {pct(n['reachable_pct'])} "
              f"({n['reports']} reports)" for n in result["nvrs"]]
    low = worst(result, "recording_pct")
    if low:
        lines += ["", "Lowest recording %:"]
        lines += [f"  {c['nvr']} ch{c['channel']} {c['name']}: {pct(c['recording_pct'])}" for c in low]
    flaps = flapping(result)
    if flaps:
        lines += ["", "Most online/offline changes:"]
        lines += [f"  {c['nvr']} ch{c['channel']} {c['name']}: {c['flaps']} changes" for c in flaps]
    return lines
