"""Monthly report as a PDF (reportlab), for people who do not use the dashboard.

Built only from data NVR Track already has: the uptime result of the month
(nvr_track.monitoring.uptime.compute over saved scan reports), the current dashboard state
(nvr_track.web.dashboard.read_dashboard_data), real-time events recorded in the month
and the open known issues. Nothing here contacts an NVR. Percentages follow
nvr_track.monitoring.uptime: failed or disabled checks are left out, never counted as
recording; no footage in a check window is not "never recorded".
"""
from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from nvr_track.nvr.storage import ABSENT_DISK_STATES, NORMAL_DISK_STATES

LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")
INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#6b7280")
LINE = colors.HexColor("#d5d9e0")
HEAD = colors.HexColor("#eef1f5")
GOOD = colors.HexColor("#15803d")
FAIR = colors.HexColor("#b45309")
BAD = colors.HexColor("#b91c1c")
ONLINE_BAR = colors.HexColor("#94a3b8")
RECORDING_BAR = colors.HexColor("#2563eb")

BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=8.6, leading=11, textColor=INK)
SMALL = ParagraphStyle("small", parent=BASE, fontSize=7.4, leading=9.4, textColor=MUTED)
CELL = ParagraphStyle("cell", parent=BASE, fontSize=7.8, leading=9.6)
TITLE = ParagraphStyle("title", parent=BASE, fontName="Helvetica-Bold", fontSize=17, leading=21)
SUB = ParagraphStyle("sub", parent=BASE, fontSize=9.5, leading=12.5, textColor=MUTED)
H2 = ParagraphStyle("h2", parent=BASE, fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=9,
                    spaceAfter=4)
KPI_NUM = ParagraphStyle("kpi", parent=BASE, fontName="Helvetica-Bold", fontSize=15, leading=18,
                         alignment=TA_CENTER)
KPI_LABEL = ParagraphStyle("kpil", parent=SMALL, alignment=TA_CENTER)


def clean(value: object, limit: int = 160) -> str:
    """Text the built-in fonts can show (Latin-1/cp1252), escaped for Paragraph."""
    text = " ".join(str(value if value is not None else "").split())[:limit]
    return escape(text.encode("cp1252", "replace").decode("cp1252"))


def pct(value: object) -> str:
    return "—" if not isinstance(value, (int, float)) else f"{value:.1f}%"


def tone(value: object, good: float = 99.0, fair: float = 90.0) -> colors.Color:
    if not isinstance(value, (int, float)):
        return MUTED
    return GOOD if value >= good else FAIR if value >= fair else BAD


def _when(text: object, pattern: str = "%d %b %Y %H:%M") -> str:
    try:
        return datetime.fromisoformat(str(text)).astimezone(LOCAL_TZ).strftime(pattern)
    except ValueError:
        return "—"


def _table(rows: list[list], widths: list[float], colored: list[tuple] = (), align_right: tuple = ()) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    style = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.8), ("FONT", (0, 1), (-1, -1), "Helvetica", 7.8),
             ("TEXTCOLOR", (0, 0), (-1, -1), INK), ("BACKGROUND", (0, 0), (-1, 0), HEAD),
             ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE), ("LINEBELOW", (0, 1), (-1, -1), 0.25, LINE),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2.6),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6), ("LEFTPADDING", (0, 0), (-1, -1), 3.5),
             ("RIGHTPADDING", (0, 0), (-1, -1), 3.5)]
    for column in align_right:
        style.append(("ALIGN", (column, 0), (column, -1), "RIGHT"))
    style += [("TEXTCOLOR", cell, cell, color) for cell, color in colored]
    table.setStyle(TableStyle(style))
    return table


def _bars(nvrs: list[dict]) -> Drawing:
    """Online and recording % per NVR (horizontal bars)."""
    rows = [n for n in nvrs if isinstance(n.get("online_pct"), (int, float))
            or isinstance(n.get("recording_pct"), (int, float))][:16]
    height = 12 * max(len(rows), 1)
    drawing = Drawing(170 * mm, height + 18)
    chart = HorizontalBarChart()
    chart.x, chart.y, chart.width, chart.height = 52, 16, 170 * mm - 140, height
    chart.data = [[n.get("online_pct") or 0 for n in reversed(rows)],
                  [n.get("recording_pct") or 0 for n in reversed(rows)]]
    chart.categoryAxis.categoryNames = [clean(n["nvr"], 20) for n in reversed(rows)]
    chart.categoryAxis.labels.fontName, chart.categoryAxis.labels.fontSize = "Helvetica", 7.5
    chart.categoryAxis.strokeColor = LINE
    chart.valueAxis.valueMin, chart.valueAxis.valueMax, chart.valueAxis.valueStep = 0, 100, 20
    chart.valueAxis.labels.fontName, chart.valueAxis.labels.fontSize = "Helvetica", 7
    chart.valueAxis.strokeColor = LINE
    chart.valueAxis.gridStrokeColor, chart.valueAxis.visibleGrid = LINE, True
    chart.bars[0].fillColor, chart.bars[1].fillColor = ONLINE_BAR, RECORDING_BAR
    chart.bars[0].strokeColor = chart.bars[1].strokeColor = None
    chart.barSpacing, chart.groupSpacing = 0.5, 3
    drawing.add(chart)
    legend = Legend()
    legend.x, legend.y = chart.x + chart.width + 10, height
    legend.fontName, legend.fontSize = "Helvetica", 7.5
    legend.colorNamePairs = [(ONLINE_BAR, "Online %"), (RECORDING_BAR, "Recording %")]
    legend.dx, legend.dy, legend.deltay = 7, 7, 11
    legend.strokeColor = None
    drawing.add(legend)
    return drawing


def attention(current: dict | None, known_channels: set[tuple[str, str]]) -> list[str]:
    """Problems now (critical reasons, disk warnings, counts) - newest saved reports."""
    if not current:
        return []
    lines = []
    for nvr in current.get("nvrs") or []:
        label = nvr["id"] + (f" {nvr['name']}" if nvr.get("name") else "")
        if nvr.get("overall_status") == "ERROR":
            lines.append(f"{label}: latest report failed.")
        lines += [f"{label}: {reason}" for reason in nvr.get("critical_reasons") or []]
        lines += [f"{label}: {w}" for w in nvr.get("warnings") or [] if w.startswith(("Disk SMART", "Storage"))]
        lines += [f"{label}: {w}" for w in nvr.get("warnings") or [] if "keep going offline" in w
                  or "keeps going offline" in w]
    counts = {"offline": 0, "not_recording": 0, "no_footage": 0}
    for nvr in current.get("nvrs") or []:
        for ch in nvr.get("channels") or []:
            if (nvr["id"], str(ch.get("channel_id"))) in known_channels:
                continue
            code = (ch.get("condition") or {}).get("code")
            counts["offline"] += ch.get("reported_status") == "OFFLINE"
            counts["not_recording"] += code == "NOT_RECORDING"
            counts["no_footage"] += code == "NO_FOOTAGE_30"
    if counts["no_footage"]:
        lines.append(f"{counts['no_footage']} cameras without footage for 30+ days (not marked as known issues).")
    if counts["not_recording"]:
        lines.append(f"{counts['not_recording']} cameras stopped recording (not marked as known issues).")
    if counts["offline"]:
        lines.append(f"{counts['offline']} cameras offline now (not marked as known issues).")
    return lines


def build(result: dict, *, period: str, period_label: str, current: dict | None = None,
          events: list[dict] | None = None, known: list[dict] | None = None, compress: bool = True) -> bytes:
    """The report as PDF bytes."""
    fleet = result.get("fleet") or {}
    nvrs = result.get("nvrs") or []
    cameras = result.get("cameras") or []
    current_nvrs = {n["id"]: n for n in (current or {}).get("nvrs") or []}
    known = known or []
    known_channels = {(str(k.get("nvr")), str(k.get("channel"))) for k in known if k.get("channel") is not None}
    generated = datetime.now(LOCAL_TZ)
    scans = sum(n.get("reports") or 0 for n in nvrs)
    story: list = []

    story += [Paragraph("NVR Track · Monthly report", TITLE),
              Paragraph(f"{clean(period_label)} · generated {generated:%d %b %Y %H:%M} · "
                        f"from {scans} saved scans of {len(nvrs)} NVRs · read-only monitoring", SUB),
              Spacer(1, 7)]

    reachable = [n["reachable_pct"] for n in nvrs if isinstance(n.get("reachable_pct"), (int, float))]
    kpis = [(str(fleet.get("cameras", 0)), "cameras"), (str(fleet.get("nvrs", 0)), "NVRs"),
            (pct(fleet.get("online_pct")), "cameras online"), (pct(fleet.get("recording_pct")), "recording found"),
            (pct(sum(reachable) / len(reachable)) if reachable else "—", "NVRs reachable"),
            ("—" if events is None else str(len(events)), "real-time events")]
    kpi_table = Table([[Paragraph(clean(v), KPI_NUM) for v, _ in kpis],
                       [Paragraph(clean(label), KPI_LABEL) for _, label in kpis]],
                      colWidths=[30 * mm] * len(kpis), hAlign="LEFT")
    kpi_table.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, 0), 0, LINE),
                                   ("LINEAFTER", (0, 0), (-2, -1), 0.5, LINE), ("BACKGROUND", (0, 0), (-1, -1),
                                                                                colors.HexColor("#f8fafc")),
                                   ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [kpi_table, Spacer(1, 4),
              Paragraph("Online % = share of scans in which the NVR reported the camera online. Recording % = "
                        "share of completed recording checks that found footage; failed or disabled checks are "
                        "left out. A camera without footage in a check window is not proof that it never "
                        "recorded.", SMALL)]

    lines = attention(current, known_channels)
    story.append(Paragraph(f"Needs attention now <font color='#6b7280' size='8'>(latest scans, "
                           f"{generated:%d %b %H:%M})</font>", H2))
    if lines:
        story += [Paragraph("• " + clean(line, 240), BASE) for line in lines[:18]]
        if len(lines) > 18:
            story.append(Paragraph(f"… and {len(lines) - 18} more on the dashboard.", SMALL))
    else:
        story.append(Paragraph("Nothing urgent in the latest scans." if current else
                               "Current state not available.", BASE))

    story.append(Paragraph("NVRs", H2))
    rows = [["NVR", "Name", "Cameras", "Reachable", "Online", "Recording", "Restarts", "Footage kept", "Now"]]
    colored = []
    for i, n in enumerate(nvrs, 1):
        now = current_nvrs.get(n["nvr"], {})
        kept = (now.get("kept_days") or {})
        kept_text = "—" if not kept.get("cameras") else (
            f"{kept['typical']}+ days" if kept["typical"] >= kept["window"] else f"{kept['typical']} days")
        rows.append([clean(n["nvr"], 20), clean(n.get("name") or now.get("name") or "", 26), n.get("cameras", 0),
                     pct(n.get("reachable_pct")), pct(n.get("online_pct")), pct(n.get("recording_pct")),
                     n.get("restarts", 0), kept_text, clean(now.get("overall_status") or "—", 12)])
        colored += [((3, i), tone(n.get("reachable_pct"))), ((4, i), tone(n.get("online_pct"), 98, 90)),
                    ((5, i), tone(n.get("recording_pct"), 98, 90)),
                    ((8, i), {"HEALTHY": GOOD, "WARNING": FAIR, "CRITICAL": BAD, "ERROR": BAD}.get(
                        now.get("overall_status"), MUTED))]
    story.append(_table(rows, [15 * mm, 34 * mm, 15 * mm, 17 * mm, 15 * mm, 18 * mm, 15 * mm, 22 * mm, 19 * mm],
                        colored, align_right=(2, 3, 4, 5, 6)))
    if nvrs:
        story += [Spacer(1, 6), _bars(nvrs)]

    low = sorted((c for c in cameras if isinstance(c.get("recording_pct"), (int, float)) and c["recording_pct"] < 98),
                 key=lambda c: (c["recording_pct"], c["nvr"], c["channel"]))
    story.append(Paragraph("Cameras with the lowest recording %", H2))
    if low:
        rows = [["NVR", "Ch", "Camera", "Online", "Recording", "Checks", "On/off changes", ""]]
        colored = []
        for i, c in enumerate(low[:25], 1):
            mark = "known issue" if (c["nvr"], c["channel"]) in known_channels else ""
            rows.append([clean(c["nvr"], 20), clean(c["channel"], 6), clean(c.get("name"), 40), pct(c["online_pct"]),
                         pct(c["recording_pct"]), c.get("checked", 0), c.get("flaps", 0), mark])
            colored += [((3, i), tone(c["online_pct"], 98, 90)), ((4, i), tone(c["recording_pct"], 98, 90)),
                        ((7, i), MUTED)]
        story.append(_table(rows, [15 * mm, 10 * mm, 56 * mm, 16 * mm, 19 * mm, 14 * mm, 22 * mm, 18 * mm],
                            colored, align_right=(1, 3, 4, 5, 6)))
        if len(low) > 25:
            story.append(Paragraph(f"{len(low) - 25} more cameras below 98% in the CSV.", SMALL))
    else:
        story.append(Paragraph("Every checked camera recorded in at least 98% of its checks.", BASE))

    flappy = sorted((c for c in cameras if (c.get("flaps") or 0) >= 4), key=lambda c: -c["flaps"])[:10]
    if flappy:
        story.append(Paragraph("Cameras going offline and online most", H2))
        rows = [["NVR", "Ch", "Camera", "On/off changes", "Online"]]
        rows += [[clean(c["nvr"], 20), clean(c["channel"], 6), clean(c.get("name"), 50), c["flaps"],
                  pct(c["online_pct"])] for c in flappy]
        story.append(_table(rows, [15 * mm, 10 * mm, 70 * mm, 25 * mm, 18 * mm], align_right=(1, 3, 4)))

    disk_rows = []
    for nvr_id, now in sorted(current_nvrs.items()):
        for disk in now.get("disks") or []:
            smart = disk.get("smart") or {}
            bad_state = str(disk.get("reported_status", "")).lower() not in NORMAL_DISK_STATES | ABSENT_DISK_STATES
            if bad_state or smart.get("status") in {"WARNING", "CRITICAL"}:
                sectors = ", ".join(f"{smart[key]} {label}" for key, label in (
                    ("reallocated", "reallocated"), ("pending", "pending"), ("uncorrectable", "uncorrectable"))
                    if isinstance(smart.get(key), (int, float)) and smart[key])
                disk_rows.append([clean(nvr_id, 20), clean(disk.get("id"), 12), clean(disk.get("reported_status"), 16),
                                  clean(smart.get("status") or "—", 12), clean(sectors or "—", 60)])
    story.append(Paragraph("Disks to replace or watch <font color='#6b7280' size='8'>(latest scans)</font>", H2))
    if disk_rows:
        story.append(_table([["NVR", "HDD", "NVR status", "SMART", "Sectors"]] + disk_rows,
                            [15 * mm, 14 * mm, 26 * mm, 20 * mm, 95 * mm]))
    else:
        story.append(Paragraph("No disk problem in the latest scans." if current else "Not available.", BASE))

    story.append(Paragraph("Real-time events in the period", H2))
    if events is None:
        story.append(Paragraph("Real-time events are not available here.", BASE))
    elif not events:
        story.append(Paragraph("None recorded.", BASE))
    else:
        counts: dict[tuple[str, str], int] = {}
        for event in events:
            key = (str(event.get("nvr")), str(event.get("label") or event.get("type")))
            counts[key] = counts.get(key, 0) + 1
        rows = [["NVR", "Event", "Count"]]
        rows += [[clean(nvr, 20), clean(label, 40), n] for (nvr, label), n in
                 sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:20]]
        story.append(_table(rows, [20 * mm, 70 * mm, 18 * mm], align_right=(2,)))

    story.append(Paragraph("Known issues open", H2))
    if known:
        rows = [["NVR", "Ch", "Reason", "Since", "Until", "By"]]
        for k in known[:25]:
            rows.append([clean(k.get("nvr"), 20), "all" if k.get("channel") is None else clean(k.get("channel"), 6),
                         Paragraph(clean(k.get("reason"), 120), CELL), _when(k.get("created_at"), "%d %b %Y"),
                         clean(k.get("until") or "until fixed", 12), clean(k.get("by"), 20)])
        story.append(_table(rows, [15 * mm, 10 * mm, 80 * mm, 22 * mm, 20 * mm, 23 * mm]))
        if len(known) > 25:
            story.append(Paragraph(f"… and {len(known) - 25} more.", SMALL))
    else:
        story.append(Paragraph("None.", BASE))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(15 * mm, 9 * mm, f"NVR Track monthly report {period}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    out = BytesIO()
    document = SimpleDocTemplate(out, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                                 bottomMargin=16 * mm, title=f"NVR Track monthly report {period}",
                                 author="NVR Track", pageCompression=1 if compress else 0)
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()


def period_label(start: datetime, end: datetime, period: str, now: datetime | None = None) -> str:
    """'October 2026 (01 Oct – 31 Oct 2026)', '…, month to date (…)' or 'Last 30 days (…)'."""
    now = now or datetime.now(LOCAL_TZ)
    start, end = start.astimezone(LOCAL_TZ), end.astimezone(LOCAL_TZ)
    last = min(end, now) - timedelta(seconds=1)
    span = f"{start:%d %b} – {last:%d %b %Y}"
    if len(period) == 7 and period[4] == "-":
        return f"{start:%B %Y}" + (", month to date" if end > now else "") + f" ({span})"
    return f"{period[:1].upper()}{period[1:]} ({span})"


def pdf_for(result: dict, period: str, compress: bool = True) -> bytes:
    """The report for an uptime result, with the current state, the period's
    real-time events and open known issues read from the local files."""
    from nvr_track.web import dashboard
    from nvr_track.nvr import events as event_store
    from nvr_track.monitoring import notes as notes_store
    start, end = datetime.fromisoformat(result["start"]), datetime.fromisoformat(result["end"])
    try:
        current = dashboard.read_dashboard_data()
    except Exception:                       # The report must not fail on the current-state part.
        current = None
    period_events = None
    if event_store.read_status():           # The listener has run here at some time.
        hours = int((datetime.now(LOCAL_TZ) - start).total_seconds() // 3600) + 1
        period_events = [e for e in event_store.recent_events(hours=max(hours, 1), limit=20000)
                         if start <= datetime.fromisoformat(e["at"]) < end]
    known = notes_store.active_issues(notes_store.load())
    return build(result, period=period, period_label=period_label(start, end, period), current=current,
                 events=period_events, known=known, compress=compress)
