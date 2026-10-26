
"""Run sequential monitoring reports for every configured NVR."""

import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

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
    recording_errors = sum(int(row.get("recording_errors", 0) or 0) for row in summary)

    subject = (
        f"[NVR Report] {counts['CRITICAL']} critical, "
        f"{counts['WARNING']} warning - {now:%Y-%m-%d}"
    )

    headers = (
        "<tr><th>NVR</th><th>IP address</th><th>Overall</th>"
        "<th>Storage</th><th>Recording</th>"
        "<th>Online</th><th>Offline</th><th>Evidence</th>"
        "<th>No recent evidence</th><th>Check errors</th></tr>"
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
            item.get("id", "—"),
            item.get("ip", "—"),
            status,
            item.get("storage", "UNKNOWN"),
            item.get("recording", "UNKNOWN"),
            item.get("online", 0),
            item.get("offline", 0),
            item.get("recording_evidence", 0),
            item.get("recording_missing", 0),
            item.get("recording_errors", 0),
        ]
        escaped = [escape(str(value)) for value in cells]
        escaped[2] = (
            f'<strong style="color:{color}">{escaped[2]}</strong>'
        )
        rows.append(
            "<tr>" + "".join(f"<td>{value}</td>" for value in escaped) + "</tr>"
        )

    html = f"""<!doctype html>
<html lang="en">
<body style="font-family:Arial,sans-serif;color:#172033">
  <h2>Daily NVR Tracking Report</h2>
  <p>Report time: {now:%Y-%m-%d %H:%M:%S %Z}</p>
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
    <tr><td>Recording search errors</td><td>{recording_errors}</td></tr>
  </table>
  <h3>Per-NVR results</h3>
  <table border="1" cellpadding="7" cellspacing="0"
    style="border-collapse:collapse;border-color:#cbd5e1;font-size:13px">
    {headers}
    {''.join(rows)}
  </table>
  <p style="color:#64748b;font-size:12px">
    Generated by NVR Tracking. See the local dashboard for detailed
    findings and individual JSON reports.
  </p>
</body>
</html>"""

    plain_lines = [
        "Daily NVR Tracking Report",
        f"Report time: {now:%Y-%m-%d %H:%M:%S %Z}",
        f"Total NVRs: {len(summary)}",
        f"Healthy: {counts['HEALTHY']}; Warnings: {counts['WARNING']}; "
        f"Critical: {counts['CRITICAL']}; Errors: {counts['ERROR']}",
        f"Cameras online: {online}; Cameras offline: {offline}",
        f"Recording evidence: {evidence}; no recent evidence: {missing_evidence}; "
        f"search errors: {recording_errors}",
        "",
    ]

    for item in summary:
        plain_lines.append(
            f"{item.get('id')} ({item.get('ip')}): "
            f"{item.get('overall')} | storage={item.get('storage')} | "
            f"recording={item.get('recording')} | "
            f"online={item.get('online')} | offline={item.get('offline')} | "
            f"evidence={item.get('recording_evidence', 0)} | "
            f"no_recent={item.get('recording_missing', 0)}"
        )

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

def main() -> int:
    try:
        nvrs = load_inventory(INVENTORY_PATH)
    except InventoryError as exc:
        print(f"Cannot load local NVR inventory: {exc}")
        return 2

    summary = []
    execution_errors = 0

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
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=max(
                    150,
                    int(os.environ.get("NVR_RECORDING_MAX_NVR_SECONDS", "300")) + 120,
                ),
            )
        except (subprocess.TimeoutExpired, ValueError):
            execution_errors += 1
            summary.append({"id": nvr.id, "ip": nvr.ip, "overall": "ERROR",
                            "storage": "UNKNOWN", "recording": "NOT_REPORTED",
                            "online": 0, "offline": 0})
            print(f"Report process timed out for {nvr.id}.", file=sys.stderr)
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
                "ip": nvr.ip,
                "overall": "ERROR",
                "storage": "UNKNOWN",
                "recording": "NOT_REPORTED",
                "online": 0,
                "offline": 0,
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
                "ip": nvr.ip,
                "overall": "ERROR",
                "storage": "UNKNOWN",
                "recording": "NOT_REPORTED",
                "online": 0,
                "offline": 0,
            })
            continue

        channel_status = report.get("channel_status", {})

        summary.append({
            "id": nvr.id,
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
        })

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

    email_sent, email_message = send_email_report(summary)
    print(f"Email report: {email_message}")

    if not email_sent and email_message.startswith("FAILED:"):
        return 2

    if execution_errors:
        return 2

    return 0 if healthy == len(nvrs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
