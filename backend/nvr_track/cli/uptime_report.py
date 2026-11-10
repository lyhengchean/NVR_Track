"""Uptime report from saved reports (no NVR contact).

  docker compose run --rm -T --entrypoint python nvr-report -m nvr_track.cli.uptime_report
  docker compose run --rm -T --entrypoint python nvr-report -m nvr_track.cli.uptime_report --month 2026-10 --email
  docker compose run --rm -T --entrypoint python nvr-report -m nvr_track.cli.uptime_report --month 2026-11 --pdf

Saves reports/uptime/uptime-<period>.csv and .json; with --pdf also the
monthly report reports/uptime/NVR-Track-<period>.pdf (nvr_track.reports.monthly_pdf), which
the email attaches and the dashboard offers for download. The nvr-scanner
service runs it for the previous month (with email and PDF) after the first
scheduled scan on the 1st; the Telegram bot posts the same report and answers
/uptime.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from nvr_track.core import mailer
from nvr_track.reports import monthly_pdf
from nvr_track.monitoring.uptime import compute, summary_lines, to_csv
from nvr_track.core.paths import PROJECT_ROOT as ROOT

REPORTS_DIR = ROOT / "reports"
TZ = ZoneInfo("Asia/Phnom_Penh")


def month_bounds(text: str) -> tuple[datetime, datetime]:
    if not re.fullmatch(r"\d{4}-\d{2}", text):
        raise ValueError("Use YYYY-MM")
    start = datetime(int(text[:4]), int(text[5:]), 1, tzinfo=TZ)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def previous_month(now: datetime | None = None) -> str:
    first = (now or datetime.now(TZ)).replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Camera online/recording uptime from saved reports.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=30, help="Last N days (default 30).")
    group.add_argument("--month", help="Calendar month YYYY-MM.")
    group.add_argument("--previous-month", action="store_true")
    parser.add_argument("--email", action="store_true", help="Email the summary with the CSV attached.")
    parser.add_argument("--pdf", action="store_true", help="Also save (and email) the monthly report as PDF.")
    args = parser.parse_args(argv)
    if args.previous_month:
        args.month = previous_month()
    if args.month:
        try:
            start, end = month_bounds(args.month)
        except ValueError as exc:
            print(exc)
            return 2
        period = args.month
    else:
        if not 1 <= args.days <= 366:
            print("--days must be 1-366")
            return 2
        end = datetime.now(TZ)
        start = end - timedelta(days=args.days)
        period = f"last{args.days}d-{end:%Y%m%d}"
    result = compute(REPORTS_DIR, start, end)
    out_dir = REPORTS_DIR / "uptime"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_text = to_csv(result)
    (out_dir / f"uptime-{period}.csv").write_text(csv_text, encoding="utf-8-sig")
    (out_dir / f"uptime-{period}.json").write_text(json.dumps({**result, "period": period}), encoding="utf-8")
    title = f"NVR Track uptime {period} ({start:%d %b %Y} – {(end - timedelta(seconds=1)):%d %b %Y})"
    lines = summary_lines(result, title)
    print("\n".join(lines))
    print(f"Saved: reports/uptime/uptime-{period}.csv")
    attachments = [(f"uptime-{period}.csv", csv_text.encode("utf-8-sig"), "text/csv")]
    if args.pdf:
        try:
            pdf = monthly_pdf.pdf_for({**result, "start": start.isoformat(), "end": end.isoformat()}, period)
        except Exception as exc:            # The CSV and email still go out.
            print(f"PDF report failed: {type(exc).__name__}")
        else:
            (out_dir / f"NVR-Track-{period}.pdf").write_bytes(pdf)
            attachments.append((f"NVR-Track-{period}.pdf", pdf, "application/pdf"))
            print(f"Saved: reports/uptime/NVR-Track-{period}.pdf ({len(pdf) // 1024} KB)")
    if args.email:
        html = "<pre style=\"font-family:Consolas,monospace\">" + escape("\n".join(lines)) + "</pre>"
        ok, message = mailer.send(f"[NVR Report] Uptime {period}", "\n".join(lines), html, attachments)
        print(f"Email report: {message}")
        if not ok and message.startswith("FAILED"):
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
