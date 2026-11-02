
"""Run sequential monitoring reports for every configured NVR."""

import argparse
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from app.camera_condition import classify
from app.config import InventoryError, load_inventory


ROOT = Path(__file__).resolve().parent.parent

# Load environment variables from the project .env file.
env_file = ROOT / ".env"

if env_file.exists():
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        elif value.startswith('"') and value.endswith('"'):
            value = value[1:-1]

        if key:
            os.environ.setdefault(key, value)

INVENTORY_PATH = ROOT / "config" / "nvrs.local.json"
REPORTS_DIR = ROOT / "reports"
FLEET_RUN_FILENAME = "fleet_latest_run.json"

# Only a confirmed per-NVR lock collision is retried. The child reporter
# exits before contacting the NVR in this condition. Keep retries bounded.
LOCK_CONFLICT_RETRIES = 2
LOCK_CONFLICT_RETRY_DELAY_SECONDS = 5

# The per-camera budget covers only recording searches. Device, channel and
# storage reads (each retried once after a network drop), the NVR-wide probe
# and the last in-flight search run outside it, so allow a fixed margin.
REPORT_TIMEOUT_MARGIN_SECONDS = 300


def report_timeout_seconds() -> int:
    """Child-process timeout; uses the reporter's own bounds and default."""
    try:
        budget = int(os.environ.get("NVR_RECORDING_MAX_NVR_SECONDS", "300"))
    except ValueError:
        budget = 300
    if not 15 <= budget <= 3600:
        budget = 300
    return budget + REPORT_TIMEOUT_MARGIN_SECONDS


def is_lock_conflict(result: subprocess.CompletedProcess, nvr_id: str) -> bool:
    """Recognize the exact safe-to-retry message from the child reporter."""
    return (result.returncode == 2
            and (result.stdout or "").strip() ==
            f"Another report is already running for {nvr_id}; no queries sent.")


def run_report_with_lock_retry(command: list[str], nvr_id: str,
                               timeout: int) -> subprocess.CompletedProcess:
    """Retry only an uncontended-NVR-safe exit; never retry device queries."""
    for attempt in range(LOCK_CONFLICT_RETRIES + 1):
        result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                text=True, check=False, timeout=timeout)
        if not is_lock_conflict(result, nvr_id):
            return result
        if attempt == LOCK_CONFLICT_RETRIES:
            print(f"Report lock remains busy for {nvr_id} after "
                  f"{attempt + 1} attempts; no queries sent.", flush=True)
            return result
        print(f"Report lock busy for {nvr_id}; retry "
              f"{attempt + 1}/{LOCK_CONFLICT_RETRIES} after "
              f"{LOCK_CONFLICT_RETRY_DELAY_SECONDS}s.", flush=True)
        time.sleep(LOCK_CONFLICT_RETRY_DELAY_SECONDS)
    raise AssertionError("Lock retry loop exhausted without result")


def save_fleet_run_status(started_at: str, rows: list[dict]) -> None:
    """Record each fleet attempt, including failures without JSON snapshots.

    Atomic replacement keeps the dashboard from reading a partially written file.
    Only fixed status/error codes and device IDs are stored; no credentials,
    exceptions, IPs or device-provided text go into the manifest.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    items = [{"id": str(row["id"]),
              "result": "ERROR" if row.get("overall") == "ERROR" else "OK",
              "reason": row.get("failure_code", "REPORT_UNAVAILABLE")
                        if row.get("overall") == "ERROR" else None}
             for row in rows]
    manifest = {"schema_version": 1, "started_at": started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "devices": items}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=REPORTS_DIR,
                                         prefix=".fleet_run_", suffix=".tmp",
                                         delete=False) as file:
            temporary = Path(file.name)
            json.dump(manifest, file, ensure_ascii=True, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, REPORTS_DIR / FLEET_RUN_FILENAME)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def camera_conditions(report: dict) -> dict:
    """Condition counts and the cameras that need action, from one saved report."""
    channels = report.get("channel_inventory", {}).get("channels", [])
    counts = {"no_footage_30": 0, "stopped_recording": 0, "gaps": 0}
    attention = []
    for channel in channels if isinstance(channels, list) else []:
        if not isinstance(channel, dict):
            continue
        condition = classify(channel)
        key = {"NO_FOOTAGE_30": "no_footage_30", "NOT_RECORDING": "stopped_recording",
               "GAPS": "gaps"}.get(condition["code"])
        if key:
            counts[key] += 1
            attention.append({"condition": condition["label"],
                              "channel": str(channel.get("channel_id", "")),
                              "camera": str(channel.get("name") or "")[:80],
                              "detail": condition["detail"]})
    return {**counts, "attention": attention}


def send_email_report(summary: list[dict]) -> tuple[bool, str]:
    """Send the consolidated NVR summary using authenticated SMTP."""

    host = os.environ.get("SMTP_HOST", "").strip()
    port_text = os.environ.get("SMTP_PORT", "587").strip()
    security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip()
    recipients = [
        address.strip()
        for address in os.environ.get("SMTP_TO", "").split(",")
        if address.strip()
    ]

    missing = [
        key for key, value in {
            "SMTP_HOST": host,
            "SMTP_USERNAME": username,
            "SMTP_PASSWORD": password,
            "SMTP_FROM": sender,
            "SMTP_TO": recipients,
        }.items()
        if not value
    ]

    if missing:
        return False, "SKIPPED: missing email configuration: " + ", ".join(missing)

    try:
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        return False, "FAILED: SMTP_PORT must be between 1 and 65535."

    if security not in {"starttls", "ssl"}:
        return False, "FAILED: SMTP_SECURITY must be starttls or ssl."

    now = datetime.now(ZoneInfo("Asia/Phnom_Penh"))
    counts = {
        status: sum(row.get("overall") == status for row in summary)
        for status in ("HEALTHY", "WARNING", "CRITICAL", "ERROR")
    }
    online = sum(int(row.get("online", 0) or 0) for row in summary)
    offline = sum(int(row.get("offline", 0) or 0) for row in summary)
    evidence = sum(int(row.get("recording_evidence", 0) or 0) for row in summary)
    missing_evidence = sum(int(row.get("recording_missing", 0) or 0) for row in summary)
    online_missing_expected = sum(int(row.get("online_missing_expected", 0) or 0) for row in summary)
    recording_errors = sum(int(row.get("recording_errors", 0) or 0) for row in summary)
    no_footage_30 = sum(int(row.get("no_footage_30", 0) or 0) for row in summary)
    stopped_recording = sum(int(row.get("stopped_recording", 0) or 0) for row in summary)
    gaps = sum(int(row.get("gaps", 0) or 0) for row in summary)

    failures = counts["ERROR"]
    successful = len(summary) - failures
    subject = (
        f"[NVR Report] {counts['CRITICAL']} critical, "
        f"{counts['WARNING']} warning, "
        f"{failures} report {'error' if failures == 1 else 'errors'} - {now:%Y-%m-%d}"
    )

    headers = (
        "<tr><th>NVR</th><th>IP address</th><th>Overall</th>"
        "<th>Storage</th><th>Online</th><th>Offline</th><th>Recording now</th>"
        "<th>Stopped recording</th><th>No footage 30+ days</th><th>Gaps</th>"
        "<th>Online but not recording</th><th>Check errors</th></tr>"
    )
    rows = []

    for item in summary:
        status = str(item.get("overall", "UNKNOWN"))
        colors = {
            "HEALTHY": "#166534",
            "WARNING": "#92400e",
            "CRITICAL": "#991b1b",
            "ERROR": "#991b1b",
        }
        color = colors.get(status, "#475569")

        cells = [
            " - ".join(str(v) for v in (item.get("id", "—"), item.get("name")) if v),
            item.get("ip", "—"),
            status,
            item.get("storage", "UNKNOWN"),
            item.get("online", 0) if status != "ERROR" else "—",
            item.get("offline", 0) if status != "ERROR" else "—",
            item.get("recording_evidence", 0) if status != "ERROR" else "—",
            item.get("stopped_recording", 0) if status != "ERROR" else "—",
            item.get("no_footage_30", 0) if status != "ERROR" else "—",
            item.get("gaps", 0) if status != "ERROR" else "—",
            item.get("online_missing_expected", 0) if status != "ERROR" else "—",
            item.get("recording_errors", 0) if status != "ERROR" else "—",
        ]
        escaped = [escape(str(value)) for value in cells]
        escaped[2] = (
            f'<strong style="color:{color}">{escaped[2]}</strong>'
        )
        rows.append(
            "<tr>" + "".join(f"<td>{value}</td>" for value in escaped) + "</tr>"
        )

    attention_rows = []
    attention_text = []
    for item in summary:
        label = " - ".join(str(v) for v in (item.get("id"), item.get("name")) if v)
        for camera in item.get("attention", [])[:100]:
            attention_rows.append(
                "<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in (
                    camera["condition"], label, camera["channel"], camera["camera"], camera["detail"])) + "</tr>")
            attention_text.append(f"{camera['condition']}: {label} ch{camera['channel']} "
                                  f"{camera['camera']} - {camera['detail']}")
    attention_html = (
        "<h3>Cameras that need action</h3>"
        '<table border="1" cellpadding="6" cellspacing="0" '
        'style="border-collapse:collapse;border-color:#cbd5e1;font-size:13px">'
        "<tr><th>Condition</th><th>NVR</th><th>Ch.</th><th>Camera</th><th>Reason</th></tr>"
        + "".join(attention_rows) + "</table>"
    ) if attention_rows else ""

    html = f"""<!doctype html>
<html lang="en">
<body style="font-family:Arial,sans-serif;color:#172033">
  <h2>Daily NVR Tracking Report</h2>
  <p>Report time: {now:%Y-%m-%d %H:%M:%S %Z}</p>
  <p><strong>Fresh reports: {successful} of {len(summary)} NVRs</strong> ·
     Report execution errors: {failures}</p>
  <p style="color:#64748b;font-size:12px">Camera totals below cover successful
     reports only. Failed devices are not counted as zero-camera devices;
     their current camera conditions are unknown.</p>
  <table cellpadding="8" cellspacing="0"
    style="border-collapse:collapse;background:#f8fafc">
    <tr><td>Total NVRs</td><td><strong>{len(summary)}</strong></td>
        <td>Healthy</td><td>{counts['HEALTHY']}</td></tr>
    <tr><td>Warnings</td><td>{counts['WARNING']}</td>
        <td>Critical</td><td>{counts['CRITICAL']}</td></tr>
    <tr><td>Cameras online</td><td>{online}</td>
        <td>Cameras offline</td><td>{offline}</td></tr>
    <tr><td>Recent recording evidence</td><td>{evidence}</td>
        <td>No recent evidence</td><td>{missing_evidence}</td></tr>
    <tr><td>Stopped recording</td><td>{stopped_recording}</td>
        <td>No footage 30+ days</td><td>{no_footage_30}</td></tr>
    <tr><td>Cameras with gaps</td><td>{gaps}</td>
        <td>Online but not recording</td><td>{online_missing_expected}</td></tr>
    <tr><td>Recording search errors</td><td>{recording_errors}</td></tr>
  </table>
  <h3>Per-NVR results</h3>
  <table border="1" cellpadding="7" cellspacing="0"
    style="border-collapse:collapse;border-color:#cbd5e1;font-size:13px">
    {headers}
    {''.join(rows)}
  </table>
  {attention_html}
  <p style="color:#64748b;font-size:12px">
    Generated by NVR Tracking. See the local dashboard for detailed
    findings and individual JSON reports.
  </p>
</body>
</html>"""

    plain_lines = [
        "Daily NVR Tracking Report",
        f"Report time: {now:%Y-%m-%d %H:%M:%S %Z}",
        f"Total NVRs: {len(summary)}; fresh reports: {successful}; report errors: {failures}",
        f"Healthy: {counts['HEALTHY']}; Warnings: {counts['WARNING']}; "
        f"Critical: {counts['CRITICAL']}; Errors: {counts['ERROR']}",
        f"Cameras online: {online}; Cameras offline: {offline}",
        f"Recording evidence: {evidence}; no recent evidence: {missing_evidence}; "
        f"online missing expected: {online_missing_expected}; search errors: {recording_errors}",
        f"Stopped recording: {stopped_recording}; no footage 30+ days: {no_footage_30}; "
        f"cameras with gaps: {gaps}",
        "",
    ]

    for item in summary:
        plain_lines.append(
            f"{' - '.join(str(v) for v in (item.get('id'), item.get('name')) if v)} ({item.get('ip')}): "
            f"{item.get('overall')} | storage={item.get('storage')} | "
            f"online={item.get('online')} | offline={item.get('offline')} | "
            f"recording_now={item.get('recording_evidence', 0)} | "
            f"stopped={item.get('stopped_recording', 0)} | "
            f"no_footage_30d={item.get('no_footage_30', 0)} | gaps={item.get('gaps', 0)}"
            f" | online_missing_expected={item.get('online_missing_expected', 0)}"
        )
    if attention_text:
        plain_lines += ["", "Cameras that need action:", *attention_text]

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content("\n".join(plain_lines))
    message.add_alternative(html, subtype="html")

    try:
        if security == "ssl":
            with smtplib.SMTP_SSL(
                host,
                port,
                timeout=25,
                context=ssl.create_default_context(),
            ) as smtp:
                smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=25) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(username, password)
                smtp.send_message(message)

    except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
        # Do not log SMTP server responses: they may echo credential material.
        return False, f"FAILED: SMTP {type(exc).__name__}; check SMTP configuration and connectivity."

    return True, f"SENT to {len(recipients)} recipient(s)."

NO_EMAIL_MESSAGE = "NOT SENT (run without email)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report on every configured NVR, one at a time.")
    parser.add_argument("--no-email", action="store_true",
                        help="Save reports but do not send the consolidated email.")
    args = parser.parse_args([] if argv is None else argv)
    try:
        nvrs = load_inventory(INVENTORY_PATH)
    except InventoryError as exc:
        print(f"Cannot load local NVR inventory: {exc}")
        return 2

    summary = []
    execution_errors = 0
    run_started_at = datetime.now(timezone.utc).isoformat()

    for nvr in nvrs:
        print(
            f"\n{'=' * 60}\n"
            f"Starting {nvr.id} at {nvr.ip}:{nvr.http_port}\n"
            f"{'=' * 60}",
            flush=True,
        )

        command = [
            sys.executable,
            "-m",
            "scripts.run_nvr_report",
            "--ip",
            nvr.ip,
            "--http-port",
            str(nvr.http_port),
        ]

        try:
            result = run_report_with_lock_retry(
                command, nvr.id, timeout=report_timeout_seconds(),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            execution_errors += 1
            failure_code = ("PROCESS_TIMEOUT" if isinstance(exc, subprocess.TimeoutExpired)
                            else "PROCESS_START_FAILURE")
            summary.append({"id": nvr.id, "name": getattr(nvr, "name", ""), "ip": nvr.ip, "overall": "ERROR",
                            "storage": "UNKNOWN", "recording": "NOT_REPORTED",
                            "online": 0, "offline": 0, "failure_code": failure_code})
            print(f"Reporter process failed for {nvr.id}: {failure_code}.",
                  file=sys.stderr)
            continue

        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")

        if result.stderr:
            # Child traceback or device diagnostics might contain secrets.
            # Do not relay raw stderr into cron, Docker or email logs.
            print(f"Reporter {nvr.id} returned diagnostics on stderr; "
                  "check the saved report status.", file=sys.stderr)

        # A report can return exit code 1 because a device is WARNING
        # or CRITICAL. That is a health finding, not a runner failure.
        report_match = re.search(
            r"^JSON report saved:\s*(.+?)\s*$",
            result.stdout,
            flags=re.MULTILINE,
        )

        if not report_match:
            execution_errors += 1
            summary.append({
                "id": nvr.id,
                "name": getattr(nvr, "name", ""),
                "ip": nvr.ip,
                "overall": "ERROR",
                "storage": "UNKNOWN",
                "recording": "NOT_REPORTED",
                "online": 0,
                "offline": 0,
                "failure_code": ("LOCK_CONFLICT" if is_lock_conflict(result, nvr.id)
                                 else "NO_SAVED_REPORT"),
            })
            continue

        report_path = Path(report_match.group(1))

        try:
            with report_path.open("r", encoding="utf-8") as file:
                report = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"Cannot read report for {nvr.id}: {exc}",
                file=sys.stderr,
            )
            execution_errors += 1
            summary.append({
                "id": nvr.id,
                "name": getattr(nvr, "name", ""),
                "ip": nvr.ip,
                "overall": "ERROR",
                "storage": "UNKNOWN",
                "recording": "NOT_REPORTED",
                "online": 0,
                "offline": 0,
                "failure_code": "REPORT_READ_ERROR",
            })
            continue

        channel_status = report.get("channel_status", {})

        summary.append({
            "id": nvr.id,
            "name": getattr(nvr, "name", ""),
            "ip": nvr.ip,
            "overall": report.get("overall", {}).get("status", "UNKNOWN"),
            "storage": report.get("storage", {}).get("status", "UNKNOWN"),
            "recording": report.get("recording", {}).get(
                "status", "UNKNOWN"
            ),
            "online": channel_status.get("online_count", 0),
            "offline": channel_status.get("offline_count", 0),
            "recording_evidence": report.get("per_camera_recording", {}).get("evidence", 0),
            "recording_missing": report.get("per_camera_recording", {}).get("no_recent_evidence", 0),
            "recording_errors": report.get("per_camera_recording", {}).get("search_errors", 0),
            "online_missing_expected": report.get("per_camera_recording", {}).get("online_missing_expected", 0),
            **camera_conditions(report),
        })

    # Always publish an attempt record, even if one NVR saved no JSON report.
    # Leave historical reports untouched and never include private inventory.
    try:
        save_fleet_run_status(run_started_at, summary)
    except OSError:
        print("WARNING: Could not save latest fleet-run status.", file=sys.stderr)

    print("\n" + "=" * 100)
    print("CONSOLIDATED NVR HEALTH SUMMARY")
    print("=" * 100)
    print(
        f"{'NVR':<10} {'IP':<17} {'OVERALL':<12} "
        f"{'STORAGE':<10} {'RECORDING':<43} {'ONLINE':>7} {'OFFLINE':>8}"
    )
    print("-" * 100)

    for item in summary:
        print(
            f"{item['id']:<10} "
            f"{item['ip']:<17} "
            f"{item['overall']:<12} "
            f"{item['storage']:<10} "
            f"{item['recording'][:42]:<43} "
            f"{item['online']:>7} "
            f"{item['offline']:>8}"
        )

    healthy = sum(item["overall"] == "HEALTHY" for item in summary)
    warnings = sum(item["overall"] == "WARNING" for item in summary)
    critical = sum(item["overall"] == "CRITICAL" for item in summary)

    print("-" * 100)
    print(
        f"Total: {len(nvrs)} | Healthy: {healthy} | "
        f"Warning: {warnings} | Critical: {critical} | "
        f"Execution errors: {execution_errors}"
    )
    print(f"Individual JSON reports: {REPORTS_DIR}")

    if args.no_email:
        email_sent, email_message = False, NO_EMAIL_MESSAGE
    else:
        email_sent, email_message = send_email_report(summary)
    print(f"Email report: {email_message}")

    if not email_sent and email_message.startswith("FAILED:"):
        return 2

    if execution_errors:
        return 2

    return 0 if healthy == len(nvrs) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
