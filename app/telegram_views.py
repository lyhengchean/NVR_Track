"""Telegram messages and change-based alerts, built from the dashboard's data.

Pure functions: no network, no files. Input is app.dashboard.read_dashboard_data()
(already whitelisted and sanitised), so the bot can never show more than the
dashboard does. Every device- or inventory-provided string is HTML-escaped.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")
MESSAGE_LIMIT = 3900          # Telegram allows 4096 characters per message.
LIST_LIMIT = 40               # Lines per list before "and N more".

HEALTH_ICON = {"HEALTHY": "🟢", "WARNING": "🟡", "CRITICAL": "🔴", "UNKNOWN": "⚪", "ERROR": "⛔"}
HEALTH_LABEL = {"HEALTHY": "Healthy", "WARNING": "Warning", "CRITICAL": "Critical",
                "UNKNOWN": "Unknown", "ERROR": "Report failed"}
CONDITION_ICON = {
    "NO_FOOTAGE_30": "⚫", "NOT_RECORDING": "🔴", "CHECK_FAILED": "🟣", "GAPS": "🟠",
    "BELOW_RETENTION": "🟠", "BAD_PICTURE": "🖼", "OFFLINE_RECORDING": "🔵", "SHORT_HISTORY": "🟢",
    "KNOWN_ISSUE": "🔕", "NO_RECENT_EVENTS": "💤", "NOT_MONITORED": "⚪", "OK": "🟢",
}
# Conditions that need someone to act (same as the dashboard's "Problems only").
PROBLEMS = ("NO_FOOTAGE_30", "NOT_RECORDING", "CHECK_FAILED", "GAPS",
            "BELOW_RETENTION", "BAD_PICTURE", "OFFLINE_RECORDING")
BAD_PICTURES = {"PICTURE_BLACK", "PICTURE_UNIFORM", "PICTURE_FROZEN", "PICTURE_VIEW_CHANGED"}
PICTURE_LABEL = {"PICTURE_OK": "OK", "PICTURE_BLACK": "black", "PICTURE_UNIFORM": "flat / covered",
                 "PICTURE_FROZEN": "frozen", "PICTURE_VIEW_CHANGED": "view changed",
                 "PICTURE_UNAVAILABLE": "not available", "PICTURE_NOT_CHECKED": "not checked",
                 "PICTURE_NOT_CONFIGURED": "not enabled"}
LIVE_LABEL = {"ONLINE": "Online", "OFFLINE": "Offline", "UNKNOWN": "Unknown",
              "NOT_RETURNED": "Not returned"}


# ---------- small helpers ----------

def esc(value: object) -> str:
    return escape(str(value if value is not None else ""), quote=False)


def when(value: object) -> str:
    try:
        moment = datetime.fromisoformat(str(value))
        if moment.tzinfo is None:
            raise ValueError
        return moment.astimezone(LOCAL_TZ).strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return "—"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def nvr_title(nvr: dict) -> str:
    """'Kiln Building (nvr-01)' or 'nvr-01'; escaped."""
    return f"{esc(nvr['name'])} ({esc(nvr['id'])})" if nvr.get("name") else esc(nvr["id"])


def camera_title(channel: dict) -> str:
    name = channel.get("name") or ""
    return f"ch{esc(channel.get('channel_id'))}" + (f" {esc(name)}" if name else "")


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split at line breaks so no part exceeds Telegram's size limit."""
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # A single very long line (never expected).
            parts.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts or [""]


def capped(lines: list[str], limit: int = LIST_LIMIT, hint: str = "") -> list[str]:
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"… and {len(lines) - limit} more{hint}"]


def find_nvr(data: dict, text: str) -> dict | None:
    """By exact id, then name, case-insensitive; '1' or '01' also find nvr-01."""
    wanted = text.strip().lower()
    if not wanted:
        return None
    nvrs = data.get("nvrs") or []
    for nvr in nvrs:
        if nvr["id"].lower() == wanted:
            return nvr
    for nvr in nvrs:
        if (nvr.get("name") or "").lower() == wanted:
            return nvr
    if wanted.isdigit():
        for nvr in nvrs:
            tail = nvr["id"].rsplit("-", 1)[-1]
            if tail.isdigit() and int(tail) == int(wanted):
                return nvr
    matches = [n for n in nvrs if wanted in (n.get("name") or "").lower()]
    return matches[0] if len(matches) == 1 else None


def last_video(channel: dict) -> str:
    recent = channel.get("recording_result") or {}
    history = channel.get("historical_recording_result") or {}
    end = recent.get("most_recent_segment_end") or history.get("most_recent_segment_end")
    if end:
        return when(end)
    last_day = (channel.get("condition") or {}).get("last_day")
    return f"on {esc(last_day)}" if last_day else "none found"


# ---------- views ----------

def scanner_line(scan: dict) -> str:
    if not scan.get("available"):
        return "⚠️ Scanner: <b>not running</b> (no automatic scans)"
    current = scan.get("current")
    if current:
        progress = current.get("progress") or {}
        if current.get("target") == "all" and progress.get("nvr"):
            return (f"🔄 Scanner: scanning {esc(progress['nvr'])}, "
                    f"{progress.get('done', 0)} of {progress.get('total', 0)}")
        return f"🔄 Scanner: scanning {esc(current.get('target'))}"
    if scan.get("queued"):
        return f"🔄 Scanner: starting a scan of {esc(scan['queued'].get('target'))}"
    nxt = scan.get("next_scheduled_at")
    if nxt:
        return f"⏱ Scanner: idle, next scan {when(nxt)}"
    return "⏱ Scanner: idle" + ("" if scan.get("scan_times") else ", no automatic scans set")


def fleet_status(data: dict) -> str:
    s = data.get("summary") or {}
    c = s.get("condition_counts") or {}
    stopped = c.get("NOT_RECORDING", 0) + c.get("NO_FOOTAGE_30", 0)
    failed = c.get("CHECK_FAILED", 0)
    if not data.get("nvrs"):
        headline = "No saved reports yet"
    elif stopped:
        headline = f"🔴 {plural(stopped, 'camera is', 'cameras are')} not recording"
    elif failed:
        headline = f"🟣 {plural(failed, 'camera', 'cameras')} could not be checked"
    else:
        headline = "✅ No monitored camera has stopped recording"
    lines = [
        f"<b>NVR Track</b> · reports from {when(s.get('last_successful_report'))}",
        f"<b>{headline}</b>",
        "",
        f"<b>NVRs</b> {s.get('latest_run_reporting', 0)} of {s.get('total_nvrs', 0)} reporting · "
        f"🟢 {s.get('healthy', 0)} · 🟡 {s.get('warning', 0)} · 🔴 {s.get('critical', 0)}"
        + (f" · ⛔ {s['latest_run_failed']} failed" if s.get("latest_run_failed") else ""),
        f"<b>Cameras</b> {s.get('cameras_online', 0)} online · {s.get('cameras_offline', 0)} offline"
        + (f" · {s['cameras_unknown']} unknown" if s.get("cameras_unknown") else ""),
        f"<b>Recording</b> {s.get('recording_evidence', 0)} found · "
        f"{s.get('recording_no_evidence', 0)} none in the window · "
        f"{max(0, s.get('recording_unverified', 0) - s.get('recording_not_checked', 0))} failed",
        f"<b>Storage</b> {s.get('storage_critical', 0)} critical · {s.get('storage_warnings', 0)} warning",
    ]
    counts = [f"{CONDITION_ICON[code]} {label} {c[code]}"
              for code, label in (("NOT_RECORDING", "Stopped"), ("NO_FOOTAGE_30", "No footage 30d"),
                                  ("CHECK_FAILED", "Check failed"), ("GAPS", "Gaps"),
                                  ("BELOW_RETENTION", "Below required days"),
                                  ("BAD_PICTURE", "Picture problem"),
                                  ("OFFLINE_RECORDING", "Offline but recording"),
                                  ("KNOWN_ISSUE", "Known issues"),
                                  ("NO_RECENT_EVENTS", "Quiet event cams"))
              if c.get(code)]
    if counts:
        lines.append("<b>Cameras needing a look</b> " + " · ".join(counts))
    stale = s.get("stale_reports", 0) - s.get("latest_run_failed", 0)
    if stale > 0:
        lines.append(f"⚠️ {plural(stale, 'NVR report is', 'NVR reports are')} out of date"
                     + (f" (missed the {when(s.get('stale_slot'))[-5:]} scan)" if s.get("stale_slot") else ""))
    extras = []
    if s.get("smart_problems"):
        extras.append(f"💽 {plural(s['smart_problems'], 'NVR')} with disk SMART problems")
    if s.get("event_streams_down"):
        extras.append(f"📡 {plural(s['event_streams_down'], 'event stream')} down")
    if s.get("events_24h"):
        extras.append(f"📟 {s['events_24h']} real-time events in 24 h (/events)")
    lines += extras
    lines.append(scanner_line(data.get("scan") or {}))
    return "\n".join(lines)


def problems_text(data: dict, nvr_id: str | None = None) -> str:
    blocks = []
    total = 0
    for nvr in data.get("nvrs") or []:
        if nvr_id and nvr["id"] != nvr_id:
            continue
        rows = sorted((ch for ch in nvr["channels"] if ch["condition"]["code"] in PROBLEMS),
                      key=lambda ch: (ch["condition"]["rank"], _channel_number(ch)))
        header = f"{HEALTH_ICON.get(nvr['overall_status'], '⚪')} <b>{nvr_title(nvr)}</b>"
        if nvr.get("last_run_failed"):
            header += " · ⛔ latest scan failed, last known results"
        notes = [f"   🔴 {esc(reason)}" for reason in nvr.get("critical_reasons", [])[:3]]
        if not rows and not notes:
            continue
        lines = [header, *notes]
        for ch in rows:
            cond = ch["condition"]
            lines.append(f"{CONDITION_ICON[cond['code']]} {camera_title(ch)} — {esc(cond['label'])}"
                         + (f" · last video {last_video(ch)}" if cond["code"] in {"NOT_RECORDING", "NO_FOOTAGE_30"} else ""))
        total += len(rows)
        blocks.append("\n".join(capped(lines, hint=" (see the dashboard)")))
    known = []
    for nvr in data.get("nvrs") or []:
        if nvr_id and nvr["id"] != nvr_id:
            continue
        if nvr.get("nvr_known_issue"):
            known.append(f"🔕 {nvr_title(nvr)} (whole NVR) — {issue_text(nvr['nvr_known_issue'])}")
        for ch in nvr["channels"]:
            if ch["condition"]["code"] == "KNOWN_ISSUE" and not (ch.get("known_issue") or {}).get("whole_nvr"):
                known.append(f"🔕 {nvr_title(nvr)} · {camera_title(ch)} — {issue_text(ch.get('known_issue'))}")
    tail = ("\n\n<b>Known issues, alerts paused</b> (" + str(len(known)) + ")\n" + "\n".join(capped(known, 25))) if known else ""
    if not blocks:
        return "✅ No camera needs action" + (f" on {esc(nvr_id)}." if nvr_id else ".") + tail
    title = f"<b>Cameras needing action</b> ({total})"
    return title + "\n\n" + "\n\n".join(blocks) + tail


def issue_text(issue: dict | None) -> str:
    if not issue:
        return "known issue"
    until = f"until {esc(issue['until'])}" if issue.get("until") else "until marked fixed"
    return f"{esc(issue.get('reason'))} ({until}" + (f", {esc(issue['by'])}" if issue.get("by") else "") + ")"


def _channel_number(channel: dict) -> int:
    text = str(channel.get("channel_id", ""))
    return int(text) if text.isdigit() else 10**6


def nvr_list(data: dict) -> str:
    lines = ["<b>NVRs</b>"]
    for nvr in data.get("nvrs") or []:
        counts = nvr.get("condition_counts") or {}
        problems = sum(counts.get(code, 0) for code in PROBLEMS)
        lines.append(
            f"{HEALTH_ICON.get(nvr['overall_status'], '⚪')} {nvr_title(nvr)} — "
            f"{nvr.get('online_channels', 0)}/{nvr.get('expected_channels') or len(nvr['channels'])} online"
            + (f" · {plural(problems, 'problem')}" if problems else "")
            + (" · out of date" if nvr.get("stale") and not nvr.get("last_run_failed") else ""))
    if len(lines) == 1:
        lines.append("No saved reports yet.")
    return "\n".join(lines)


def nvr_detail(data: dict, nvr: dict, show_ips: bool) -> str:
    counts = nvr.get("condition_counts") or {}
    pc = nvr.get("per_camera_recording") or {}
    lines = [f"{HEALTH_ICON.get(nvr['overall_status'], '⚪')} <b>{nvr_title(nvr)}</b> · "
             f"{HEALTH_LABEL.get(nvr['overall_status'], esc(nvr['overall_status']))}"]
    meta = [esc(x) for x in ((nvr.get("ip") if show_ips else None), nvr.get("model"),
                             ("Firmware " + nvr["firmware_version"]) if nvr.get("firmware_version") else None) if x]
    if meta:
        lines.append(" · ".join(meta))
    lines.append(f"Report {when(nvr.get('last_check'))}"
                 + (" · ⚠️ out of date" if nvr.get("stale") else ""))
    if nvr.get("last_run_failed"):
        lines.append(f"⛔ Latest scan failed ({esc(nvr.get('last_run_failure_code'))}); "
                     "results below are the last known ones.")
    lines.append(f"Cameras {nvr.get('online_channels', 0)} online · {nvr.get('offline_channels', 0)} offline"
                 + (f" · {nvr['unknown_channels']} unknown" if nvr.get("unknown_channels") else "")
                 + f" · {nvr.get('configured_channels') if nvr.get('configured_channels') is not None else '—'}"
                 f" of {nvr.get('expected_channels') if nvr.get('expected_channels') is not None else '—'} channels")
    shown = [f"{CONDITION_ICON[code]} {esc(code_label)} {counts[code]}"
             for code, code_label in (("NOT_RECORDING", "Stopped"), ("NO_FOOTAGE_30", "No footage 30d"),
                                      ("CHECK_FAILED", "Check failed"), ("GAPS", "Gaps"),
                                      ("BELOW_RETENTION", "Below required days"),
                                      ("OFFLINE_RECORDING", "Offline but recording"),
                                      ("NO_RECENT_EVENTS", "Quiet event cams"),
                                      ("NOT_MONITORED", "Not monitored"), ("OK", "OK"))
             if counts.get(code)]
    if shown:
        lines.append(" · ".join(shown))
    lines.append(f"Storage: {HEALTH_ICON.get(nvr['storage_status'], '⚪')} {esc(nvr['storage_status'].title())}"
                 + (f" — {esc(nvr['storage_message'])}" if nvr.get("storage_message") else ""))
    if not pc.get("enabled"):
        lines.append("Per-camera recording checks are not enabled for this NVR.")
    smart = nvr.get("smart") or {}
    if smart.get("status") not in {None, "NOT_CONFIGURED"}:
        lines.append(f"Disk SMART: {esc(smart['status'].title())}"
                     + (f" — {esc(smart.get('message'))}" if smart.get("status") != "HEALTHY" else ""))
    if isinstance(nvr.get("uptime_seconds"), (int, float)):
        up = int(nvr["uptime_seconds"])
        lines.append(f"Uptime {up // 86400} d {up % 86400 // 3600} h"
                     + (" · 🔁 restarted since the previous scan" if (nvr.get("restart") or {}).get("detected") else ""))
    changes = (nvr.get("inventory_changes") or {}).get("changes") or []
    if changes:
        lines.append(f"🔀 {plural(len(changes), 'camera list change')} since the previous report")
    stream = nvr.get("event_stream")
    if stream:
        lines.append("📡 Event stream " + ("connected" if stream.get("connected") else
                                          f"<b>down</b> ({esc(stream.get('last_error') or '?')})")
                     + (f" · {plural(len(nvr.get('events') or []), 'event')} in 24 h" if nvr.get("events") else ""))
    if nvr.get("nvr_known_issue"):
        lines.append(f"🔕 Whole-NVR known issue: {issue_text(nvr['nvr_known_issue'])}")
    offset = pc.get("nvr_clock_offset_seconds_approx")
    if isinstance(offset, int) and offset:
        lines.append(f"NVR clock offset: {offset:+d} s")
    findings = ([f"🔴 {esc(t)}" for t in nvr.get("critical_reasons", [])]
                + [f"🟡 {esc(t)}" for t in nvr.get("warnings", [])]
                + [f"⚪ {esc(t)}" for t in nvr.get("unknown_checks", [])])
    if findings:
        lines += ["", "<b>Findings</b>", *capped(findings, 10)]
    return "\n".join(lines)


def camera_hits(data: dict, query: str) -> list[tuple[dict, dict]]:
    """Cameras matching a name, 'ch5', a channel number, a camera IP or 'nvr-01 5'."""
    text = query.strip().lower()
    if not text:
        return []
    nvr_part, _, channel_part = text.partition(" ")
    pinned = find_nvr(data, nvr_part) if channel_part.strip().lstrip("ch").isdigit() else None
    hits = []
    for nvr in data.get("nvrs") or []:
        for ch in nvr["channels"]:
            cid = str(ch.get("channel_id", "")).lower()
            if pinned is not None:
                if nvr is pinned and cid == channel_part.strip().lstrip("ch"):
                    hits.append((nvr, ch))
                continue
            if (text in (ch.get("name") or "").lower() or text in {cid, "ch" + cid}
                    or (ch.get("camera_ip") and text == ch["camera_ip"].lower())):
                hits.append((nvr, ch))
    return hits


def camera_search(data: dict, query: str, show_ips: bool, limit: int = 12) -> str:
    """Search camera names, 'ch5', channel numbers, camera IPs and 'nvr-01 5'."""
    if not query.strip():
        return "Send a camera name, channel (ch5), camera IP or 'nvr-01 5'."
    hits = camera_hits(data, query)
    if not hits:
        return f"No camera matches <b>{esc(query.strip())}</b>."
    blocks = []
    for nvr, ch in hits[:limit]:
        cond = ch["condition"]
        days = (ch.get("retention_result") or {}).get("days_with_footage")
        window = (ch.get("retention_result") or {}).get("window_days")
        block = [f"{CONDITION_ICON[cond['code']]} <b>{camera_title(ch)}</b> · {nvr_title(nvr)}",
                 f"{esc(cond['label'])} — {esc(cond['detail'])}",
                 f"Live: {esc(LIVE_LABEL.get(ch['reported_status'], ch['reported_status']))}"
                 f" · Last video: {last_video(ch)}"
                 + (f" · Recorded days: {days}/{window}" if isinstance(days, int) and window else "")]
        picture = (ch.get("snapshot_result") or {}).get("status")
        if picture and picture != "PICTURE_NOT_CONFIGURED":
            block.append(f"Picture: {PICTURE_LABEL.get(picture, esc(picture))}")
        if ch.get("known_issue"):
            block.append(f"🔕 Known issue: {issue_text(ch['known_issue'])}")
        for note in (ch.get("notes") or [])[:2]:
            block.append(f"📝 {when(note.get('at'))} {esc(note.get('text'))}"
                         + (f" ({esc(note['by'])})" if note.get("by") else ""))
        if show_ips and ch.get("camera_ip"):
            block.append(f"IP {esc(ch['camera_ip'])}")
        if nvr.get("last_run_failed"):
            block.append("⛔ Last known result (latest scan of this NVR failed)")
        blocks.append("\n".join(block))
    more = f"\n\n… and {len(hits) - limit} more; be more specific." if len(hits) > limit else ""
    return "\n\n".join(blocks) + more


def offline_text(data: dict, nvr_id: str | None = None) -> str:
    blocks, total = [], 0
    for nvr in data.get("nvrs") or []:
        if nvr_id and nvr["id"] != nvr_id:
            continue
        rows = sorted((ch for ch in nvr["channels"] if ch["reported_status"] == "OFFLINE"),
                      key=_channel_number)
        if not rows:
            continue
        total += len(rows)
        items = [camera_title(ch) + (" (still recording)" if ch["condition"]["code"] == "OFFLINE_RECORDING" else "")
                 for ch in rows]
        blocks.append(f"📴 <b>{nvr_title(nvr)}</b> ({len(rows)})\n" + "\n".join(capped(items, 30)))
    if not blocks:
        return "✅ No camera is reported offline" + (f" on {esc(nvr_id)}." if nvr_id else ".")
    return f"<b>Offline cameras</b> ({total})\n\n" + "\n\n".join(blocks)


def _space(mb: object) -> str:
    try:
        n = float(mb)
    except (TypeError, ValueError):
        return "—"
    return f"{n / 1e6:.1f} TB" if n >= 1e6 else f"{n / 1e3:.0f} GB"


def storage_text(data: dict) -> str:
    lines = ["<b>Storage</b>"]
    for nvr in data.get("nvrs") or []:
        lines.append(f"{HEALTH_ICON.get(nvr['storage_status'], '⚪')} <b>{nvr_title(nvr)}</b> "
                     f"{esc(nvr['storage_status'].title())}")
        if nvr["storage_status"] != "HEALTHY" and nvr.get("storage_message"):
            lines.append(f"   {esc(nvr['storage_message'])}")
        smart = nvr.get("smart") or {}
        if smart.get("status") not in {None, "NOT_CONFIGURED"}:
            temps = [d["temperature_c"] for d in smart.get("disks", []) if isinstance(d.get("temperature_c"), (int, float))]
            lines.append(f"   SMART {esc(smart['status'].lower())}"
                         + (f", max {max(temps):.0f} °C" if temps else "")
                         + (f" — {esc(smart.get('message'))}" if smart["status"] in {"WARNING", "CRITICAL"} else ""))
        for disk in nvr.get("disks") or []:
            state = str(disk.get("reported_status", "")).lower()
            try:
                used = round(100 * (1 - float(disk["free_space_mb"]) / float(disk["capacity_mb"])))
            except (TypeError, ValueError, KeyError, ZeroDivisionError):
                used = None
            if state in {"ok", "normal", "idle"} and (used is None or used < 99):
                continue
            lines.append(f"   HDD {esc(disk.get('id'))}: {esc(state or 'unknown')}, "
                         f"{_space(disk.get('capacity_mb'))}"
                         + (f", {used}% used" if used is not None else ""))
    if len(lines) == 1:
        lines.append("No saved reports yet.")
    return "\n".join(lines)


def firmware_text(data: dict) -> str:
    lines = ["<b>Models and firmware</b>"]
    for nvr in data.get("nvrs") or []:
        lines.append(f"{nvr_title(nvr)} — {esc(nvr.get('model') or 'model unknown')}"
                     f" · {esc(nvr.get('firmware_version') or 'firmware unknown')}")
    versions: dict[tuple, int] = {}
    for nvr in data.get("nvrs") or []:
        key = (nvr.get("model") or "?", nvr.get("firmware_version") or "?")
        versions[key] = versions.get(key, 0) + 1
    if len(versions) > 1:
        lines += ["", "<b>Distinct versions</b>"]
        lines += [f"{esc(m)} · {esc(f)}: {plural(n, 'NVR')}" for (m, f), n in sorted(versions.items())]
    lines += ["", "Compare these with Hikvision security advisories before updating."]
    return "\n".join(lines)


def scan_text(scan: dict) -> str:
    lines = ["<b>Scanner</b>", scanner_line(scan)]
    if scan.get("scan_times"):
        lines.append("Automatic scans: " + ", ".join(esc(t) for t in scan["scan_times"]))
    if scan.get("email_times"):
        lines.append("Email from the " + ", ".join(esc(t) for t in scan["email_times"]) + " scan")
    last = scan.get("last")
    if last:
        lines.append(f"Last scan: {esc(last.get('target'))}, {esc((last.get('result') or 'unknown').lower())}, "
                     f"{when(last.get('finished_at'))}")
    if scan.get("cooldown_seconds"):
        lines.append(f"Next manual scan possible in {scan['cooldown_seconds']} s")
    if not scan.get("on_demand_enabled"):
        lines.append("Manual scans are turned off (NVR_ON_DEMAND_SCANS).")
    return "\n".join(lines)


def scan_finished_text(last: dict) -> str:
    target = "all NVRs" if last.get("target") == "all" else esc(last.get("target"))
    result = last.get("result")
    if result == "OK":
        return f"✅ Scan of {target} finished" + (
            f": {HEALTH_LABEL.get(last.get('overall'), 'see /status')}." if last.get("overall") else ". See /status.")
    if result == "ERRORS":
        return f"🟡 Scan of {target} finished; {plural(last.get('execution_errors') or 0, 'NVR')} could not be checked."
    if result == "BUSY":
        return f"🟡 {target} was already being scanned. Try again in a minute."
    return f"⛔ Scan of {target} did not complete ({esc((result or 'unknown').lower())})."


# ---------- change-based alerts ----------

def _entry(severity: str, text: str, resolved: str, value: str = "", event: bool = False) -> dict:
    entry = {"severity": severity, "text": text, "resolved": resolved, "value": value}
    if event:
        entry["event"] = True  # One-off: announced once, never "resolved".
    return entry


NVR_PREFIXES = ("nvr:{0}:", "cam:{0}:", "storage:{0}", "clock:{0}", "checks:{0}", "smart:{0}",
                "events:{0}:", "change:{0}:", "restart:{0}:")


def alert_state(data: dict, *, clock_drift_seconds: int = 120) -> tuple[dict, list[str], list[str]]:
    """Current alert conditions, prefixes whose state is unknown, and muted prefixes.

    Keys under an unknown prefix keep their previous state, so a failed scan
    or a camera whose check failed never produces a false "resolved" (or a
    false new alert) message. Keys under a muted prefix (known issues,
    features switched off) are dropped silently.
    """
    state: dict[str, dict] = {}
    unknown: list[str] = []
    muted: list[str] = []
    scan = data.get("scan") or {}
    if not scan.get("available"):
        state["scanner:down"] = _entry(
            "critical", "⚠️ <b>Scanner stopped</b> — no automatic scans are running. "
            "On the server: docker compose logs --tail 50 nvr-scanner",
            "✅ <b>Scanner running again</b>")
    for nvr in data.get("nvrs") or []:
        nid = nvr["id"]
        title = nvr_title(nvr)
        if nvr.get("nvr_known_issue"):
            muted += [p.format(nid) for p in NVR_PREFIXES]
            continue
        stream = nvr.get("event_stream")
        if stream is None:
            muted.append(f"events:{nid}:")
        elif not stream.get("connected"):
            state[f"events:{nid}:down"] = _entry(
                "warning", f"📡 <b>{title}</b>: real-time event stream disconnected "
                f"({esc(stream.get('last_error') or 'no reply')})",
                f"✅ <b>{title}</b>: real-time events connected again")
        if nvr.get("last_run_failed") or nvr["overall_status"] == "ERROR":
            state[f"nvr:{nid}:failed"] = _entry(
                "critical", f"⛔ <b>{title}</b> could not be scanned "
                f"({esc(nvr.get('last_run_failure_code') or 'no report')}). Is the NVR reachable?",
                f"✅ <b>{title}</b> is reporting again")
            unknown.append(f"nvr:{nid}:")  # Everything else about it is last-known data.
            unknown += [f"cam:{nid}:", f"storage:{nid}", f"clock:{nid}", f"checks:{nid}", f"smart:{nid}"]
            continue
        if nvr.get("stale"):
            state[f"nvr:{nid}:stale"] = _entry(
                "warning", f"⚠️ <b>{title}</b>: no new report since {when(nvr.get('last_check'))}",
                f"✅ <b>{title}</b> has a fresh report")
            unknown += [f"cam:{nid}:", f"storage:{nid}", f"clock:{nid}", f"checks:{nid}", f"nvr:{nid}:critical",
                        f"smart:{nid}"]
            continue
        if nvr["overall_status"] == "CRITICAL":
            reason = (nvr.get("critical_reasons") or ["see /nvr " + nid])[0]
            state[f"nvr:{nid}:critical"] = _entry(
                "critical", f"🔴 <b>{title}</b> is critical: {esc(reason)}",
                f"✅ <b>{title}</b> is no longer critical")
        if nvr["storage_status"] in {"CRITICAL", "WARNING"}:
            icon = "🔴" if nvr["storage_status"] == "CRITICAL" else "🟡"
            state[f"storage:{nid}"] = _entry(
                "critical" if icon == "🔴" else "warning",
                f"{icon} <b>{title}</b> storage {nvr['storage_status'].lower()}: {esc(nvr.get('storage_message'))}",
                f"✅ <b>{title}</b> storage healthy again", value=nvr["storage_status"])
        elif nvr["storage_status"] != "HEALTHY":
            unknown.append(f"storage:{nid}")
        smart = (nvr.get("smart") or {}).get("status")
        if smart in {"WARNING", "CRITICAL"}:
            state[f"smart:{nid}"] = _entry(
                "critical" if smart == "CRITICAL" else "warning",
                f"💽 <b>{title}</b> disk SMART {smart.lower()}: {esc(nvr['smart'].get('message'))}",
                f"✅ <b>{title}</b> disks report SMART OK again", value=smart)
        elif smart in {None, "NOT_CONFIGURED"}:
            muted.append(f"smart:{nid}")
        elif smart != "HEALTHY":
            unknown.append(f"smart:{nid}")
        offset = (nvr.get("per_camera_recording") or {}).get("nvr_clock_offset_seconds_approx")
        if isinstance(offset, int) and abs(offset) > clock_drift_seconds:
            state[f"clock:{nid}"] = _entry(
                "warning", f"🕒 <b>{title}</b> clock is {abs(offset)} s "
                f"{'ahead' if offset > 0 else 'behind'}; recording times may be wrong",
                f"✅ <b>{title}</b> clock is correct again")
        elif not isinstance(offset, int):
            unknown.append(f"clock:{nid}")
        restart = nvr.get("restart") or {}
        if restart.get("detected"):
            state[f"restart:{nid}:{restart.get('boot_time')}"] = _entry(
                "warning", f"🔁 <b>{title}</b> restarted at about {when(restart.get('boot_time'))}", "", event=True)
        kinds = {"added": "➕ Camera added", "removed": "➖ Camera removed",
                 "renamed": "✏️ Camera renamed", "ip_changed": "🔀 Camera IP changed"}
        for index, change in enumerate((nvr.get("inventory_changes") or {}).get("changes") or []):
            names = " → ".join(esc(v) for v in (change.get("old"), change.get("new"))
                               if v and change["type"] != "ip_changed")
            state[f"change:{nid}:{nvr.get('last_check')}:{index}"] = _entry(
                "warning", f"{kinds.get(change['type'], 'Camera changed')}: <b>{title}</b> · "
                f"ch{esc(change.get('channel_id'))}" + (f" {names}" if names else ""), "", event=True)
        if not nvr["channels"]:
            unknown.append(f"cam:{nid}:")
        failed_checks = 0
        for ch in nvr["channels"]:
            key = f"cam:{nid}:{ch['channel_id']}:"
            if ch.get("known_issue"):
                muted.append(key)  # Someone is on it: no alerts until fixed or expired.
                continue
            code = ch["condition"]["code"]
            camera = f"<b>{title}</b> · {camera_title(ch)}"
            if code in {"CHECK_FAILED", "NOT_MONITORED"}:
                failed_checks += code == "CHECK_FAILED"
                unknown += [key + "rec", key + "ret"]
            elif code in {"NOT_RECORDING", "NO_FOOTAGE_30"}:
                state[key + "rec"] = _entry(
                    "critical", f"{CONDITION_ICON[code]} {esc(ch['condition']['label'])}: {camera}\n"
                    f"   {esc(ch['condition']['detail'])}",
                    f"✅ Recording again: {camera}")
            elif code == "BELOW_RETENTION":
                state[key + "ret"] = _entry(
                    "warning", f"🟠 Below required days: {camera}\n   {esc(ch['condition']['detail'])}",
                    f"✅ Keeps the required days again: {camera}")
            picture = (ch.get("snapshot_result") or {}).get("status")
            if picture in BAD_PICTURES:
                state[key + "pic"] = _entry(
                    "warning", f"🖼 Picture {PICTURE_LABEL[picture]}: {camera}\n"
                    f"   {esc((ch.get('snapshot_result') or {}).get('message'))}",
                    f"✅ Picture OK again: {camera}", value=picture)
            elif picture in {None, "PICTURE_NOT_CONFIGURED"}:
                muted.append(key + "pic")
            elif picture != "PICTURE_OK":
                unknown.append(key + "pic")
            if ch["reported_status"] == "OFFLINE":
                state[key + "off"] = _entry("warning", f"📴 Went offline: {camera}", f"✅ Back online: {camera}")
            elif ch["reported_status"] != "ONLINE":
                unknown.append(key + "off")
        if failed_checks:
            state[f"checks:{nid}"] = _entry(
                "warning", f"🟣 <b>{title}</b>: {plural(failed_checks, 'camera check')} could not be completed",
                f"✅ <b>{title}</b>: all camera checks completed again")
    return state, unknown, muted


def compare(previous: dict, current: dict, unknown: list[str], known_nvrs: set[str],
            muted: list[str] = ()) -> tuple[dict, list[dict], list[dict]]:
    """Merge with the previous state; return (state to save, opened, resolved)."""
    merged = dict(current)
    for key, entry in previous.items():
        if key in merged or any(key.startswith(prefix) for prefix in muted) or entry.get("event"):
            continue  # Muted or one-off: dropped silently.
        parts = key.split(":")
        nvr_gone = len(parts) > 1 and parts[0] in {"nvr", "cam", "storage", "clock", "checks", "smart", "events"} \
            and parts[1] not in known_nvrs
        if nvr_gone or any(key.startswith(prefix) for prefix in unknown):
            merged[key] = entry  # State unknown now: neither new nor resolved.
    opened = [entry for key, entry in merged.items()
              if key not in previous or previous[key].get("value") != entry.get("value")]
    resolved = [entry for key, entry in previous.items()
                if key not in merged and not entry.get("event")
                and not any(key.startswith(prefix) for prefix in muted)]
    return merged, opened, resolved


def alert_message(opened: list[dict], resolved: list[dict]) -> str:
    rank = {"critical": 0, "warning": 1, "info": 2}
    lines = [f"<b>NVR Track alerts</b> · {datetime.now(LOCAL_TZ):%d %b %H:%M}"]
    if opened:
        lines += [""] + capped([e["text"] for e in sorted(opened, key=lambda e: rank.get(e["severity"], 3))],
                               hint=" — send /problems for the full list")
    if resolved:
        lines += [""] + capped([e["resolved"] for e in resolved], hint="")
    return "\n".join(lines)


# ---------- add-on views ----------

def events_text(data: dict, nvr_id: str | None = None, limit: int = 30) -> str:
    rows, streams = [], []
    for nvr in data.get("nvrs") or []:
        if nvr_id and nvr["id"] != nvr_id:
            continue
        stream = nvr.get("event_stream")
        if stream:
            streams.append(f"{'📡' if stream.get('connected') else '⚠️'} {nvr_title(nvr)} "
                           + ("connected" if stream.get("connected") else f"down ({esc(stream.get('last_error') or '?')})"))
        for event in nvr.get("events") or []:
            rows.append((event.get("at", ""), f"{'🔴' if event.get('severity') == 'critical' else '🟡'} "
                         f"{when(event.get('at'))} {nvr_title(nvr)}"
                         + (f" · ch{esc(event['channel'])}" if event.get("channel") else "")
                         + f" — {esc(event.get('label'))}"))
    if not streams and not rows:
        return ("No real-time events. To enable them: run scripts.probe_nvr_features, "
                "set NVR_EVENT_NVR_IDS in .env and start nvr-events.")
    rows.sort(reverse=True)
    lines = ["<b>Real-time events, last 24 h</b>", *streams, ""]
    lines += capped([r[1] for r in rows], limit, " older") if rows else ["No events in the last 24 hours."]
    return "\n".join(lines)


def notes_text(data: dict, nvr: dict, channel: str | None) -> str:
    rows = []
    if channel is None:
        rows = nvr.get("nvr_notes") or []
        for ch in nvr["channels"]:
            rows += [{**n, "channel": ch["channel_id"]} for n in ch.get("notes") or []]
        rows.sort(key=lambda n: n.get("at") or "", reverse=True)
        title = f"<b>Notes · {nvr_title(nvr)}</b>"
    else:
        ch = next((c for c in nvr["channels"] if str(c["channel_id"]) == channel), None)
        if ch is None:
            return f"No channel {esc(channel)} on {nvr_title(nvr)}."
        rows = ch.get("notes") or []
        title = f"<b>Notes · {nvr_title(nvr)} · {camera_title(ch)}</b>"
        if ch.get("known_issue"):
            title += f"\n🔕 {issue_text(ch['known_issue'])}"
    if not rows:
        return title + "\nNo notes yet. Admins add one with /note."
    return title + "\n" + "\n".join(capped([
        f"📝 {when(n.get('at'))}" + (f" ch{esc(n['channel'])}" if channel is None and n.get("channel") else "")
        + f" {esc(n.get('text'))}" + (f" ({esc(n['by'])})" if n.get("by") else "") for n in rows], 20))


def known_list(data: dict) -> str:
    lines = []
    for nvr in data.get("nvrs") or []:
        if nvr.get("nvr_known_issue"):
            lines.append(f"🔕 {nvr_title(nvr)} (whole NVR) — {issue_text(nvr['nvr_known_issue'])}")
        for ch in nvr["channels"]:
            if ch.get("known_issue") and not ch["known_issue"].get("whole_nvr"):
                lines.append(f"🔕 {nvr_title(nvr)} · {camera_title(ch)} — {issue_text(ch['known_issue'])}")
    if not lines:
        return ("No known issues. Admins: /known nvr-01 5 7d Cable cut — pauses alerts for that camera; "
                "/fixed nvr-01 5 resumes them.")
    return f"<b>Known issues, alerts paused</b> ({len(lines)})\n" + "\n".join(capped(lines, 50))


def uptime_text(result: dict, title: str) -> str:
    def pct(v):
        return "—" if v is None else f"{v:.1f}%"
    fleet = result["fleet"]
    lines = [f"<b>{esc(title)}</b>",
             f"{fleet['cameras']} cameras on {fleet['nvrs']} NVRs · online {pct(fleet['online_pct'])} · "
             f"recording {pct(fleet['recording_pct'])}", ""]
    for n in result["nvrs"]:
        lines.append(f"{esc(n['nvr'])}{' ' + esc(n['name']) if n['name'] else ''}: online {pct(n['online_pct'])}, "
                     f"recording {pct(n['recording_pct'])}, reachable {pct(n['reachable_pct'])}")
    low = sorted((c for c in result["cameras"] if c["recording_pct"] is not None and c["recording_pct"] < 100),
                 key=lambda c: c["recording_pct"])[:10]
    if low:
        lines += ["", "<b>Lowest recording %</b>"]
        lines += [f"{esc(c['nvr'])} ch{esc(c['channel'])} {esc(c['name'])}: {pct(c['recording_pct'])}" for c in low]
    flaps = sorted((c for c in result["cameras"] if c["flaps"] >= 3), key=lambda c: -c["flaps"])[:10]
    if flaps:
        lines += ["", "<b>Most online/offline changes</b>"]
        lines += [f"{esc(c['nvr'])} ch{esc(c['channel'])} {esc(c['name'])}: {c['flaps']}" for c in flaps]
    return "\n".join(lines)


def event_alert_text(lines: list[dict], data: dict) -> str:
    titles = {nvr["id"]: nvr_title(nvr) for nvr in data.get("nvrs") or []}
    names = {(nvr["id"], str(ch["channel_id"])): camera_title(ch)
             for nvr in data.get("nvrs") or [] for ch in nvr["channels"]}
    rows = [f"{'🔴' if e.get('severity') == 'critical' else '🟡'} <b>{esc(e.get('label'))}</b> — "
            f"{titles.get(e['nvr'], esc(e['nvr']))}"
            + (f" · {names.get((e['nvr'], e['channel']), 'ch' + esc(e['channel']))}" if e.get("channel") else "")
            + f" ({when(e.get('at'))})" for e in lines]
    return "<b>📟 NVR events</b>\n" + "\n".join(capped(rows, 30, " — see /events"))
