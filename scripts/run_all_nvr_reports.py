
"""Run sequential monitoring reports for every configured NVR."""

import json
import re
import subprocess
import sys
from pathlib import Path

from app.config import InventoryError, load_inventory


ROOT = Path(__file__).resolve().parent.parent
INVENTORY_PATH = ROOT / "config" / "nvrs.local.json"
REPORTS_DIR = ROOT / "reports"


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

        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")

        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")

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

    if execution_errors:
        return 2

    return 0 if healthy == len(nvrs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
