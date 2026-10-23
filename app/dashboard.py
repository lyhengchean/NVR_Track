"""Read-only, password-protected NVR monitoring dashboard."""

import base64
import hmac
import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "reports"))
HOST = "0.0.0.0"
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NVR Tracking Dashboard</title>
<style>
:root {
  color-scheme: light;
  font-family: Inter, "Segoe UI", Arial, sans-serif;
  background: #f3f6fb;
  color: #172033;
}
* { box-sizing: border-box; }
body { margin: 0; }
header {
  background: #172554;
  color: #fff;
  padding: 22px max(20px, calc((100% - 1440px) / 2));
}
header h1 { margin: 0 0 6px; font-size: 25px; }
header p { margin: 0; color: #dbeafe; }
main { max-width: 1440px; padding: 22px; margin: auto; }
.toolbar {
  display: flex; flex-wrap: wrap; gap: 12px;
  align-items: center; justify-content: space-between;
  margin-bottom: 18px;
}
#search {
  min-width: 230px; flex: 1; max-width: 480px;
  padding: 11px 13px; border: 1px solid #cbd5e1;
  border-radius: 9px; background: #fff; font-size: 15px;
}
button {
  border: 0; border-radius: 9px; padding: 11px 16px;
  cursor: pointer; background: #2563eb; color: white;
  font-weight: 600;
}
button:focus-visible, input:focus-visible {
  outline: 3px solid #93c5fd; outline-offset: 2px;
}
#refresh-time, #message { color: #64748b; font-size: 13px; }
#message { margin: 0 0 14px; }
.stats {
  display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr));
  gap: 12px; margin-bottom: 22px;
}
.stat, .nvr-card {
  background: white; border: 1px solid #e2e8f0;
  border-radius: 12px; box-shadow: 0 2px 8px #0f172a08;
}
.stat { padding: 16px; }
.stat .label { color: #64748b; font-size: 13px; }
.stat .value { margin-top: 6px; font-size: 27px; font-weight: 750; }
#nvr-list { display: grid; grid-template-columns: 1fr; gap: 14px; }
.nvr-card { padding: 18px; min-width: 0; }
.nvr-head {
  display: flex; flex-wrap: wrap; align-items: flex-start;
  justify-content: space-between; gap: 10px;
}
.nvr-head h2 { font-size: 18px; margin: 0 0 5px; }
.subline { color: #64748b; font-size: 13px; overflow-wrap: anywhere; }
.badges { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 12px; }
.pill {
  display: inline-block; border-radius: 999px;
  padding: 5px 9px; font-size: 12px; font-weight: 700;
  background: #e2e8f0; color: #334155;
}
.pill.healthy { background: #dcfce7; color: #166534; }
.pill.warning { background: #fef3c7; color: #92400e; }
.pill.critical { background: #fee2e2; color: #991b1b; }
.pill.unknown { background: #e2e8f0; color: #475569; }
.metrics {
  display: grid; grid-template-columns: repeat(4, minmax(100px, 1fr));
  gap: 10px; margin: 16px 0;
}
.metric { background: #f8fafc; border-radius: 8px; padding: 12px; }
.metric .label { color: #64748b; font-size: 12px; }
.metric .value { font-weight: 700; margin-top: 5px; overflow-wrap: anywhere; }
details { margin-top: 12px; border-top: 1px solid #e2e8f0; padding-top: 13px; }
summary { cursor: pointer; font-size: 14px; font-weight: 650; }
.detail-grid {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px; margin-top: 16px;
}
h3 { font-size: 14px; margin: 0 0 9px; }
.findings { padding-left: 19px; margin: 8px 0; }
.findings li { margin: 7px 0; overflow-wrap: anywhere; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td {
  padding: 9px 8px; border-bottom: 1px solid #e2e8f0;
  text-align: left; vertical-align: top; overflow-wrap: anywhere;
}
th { color: #64748b; font-weight: 650; background: #f8fafc; }
.table-wrap { overflow-x: auto; }
.muted { color: #64748b; font-size: 13px; }
.error { color: #b91c1c; }
footer { margin: 28px 0 6px; color: #64748b; font-size: 12px; }
@media (max-width: 1000px) {
  .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 650px) {
  main { padding: 14px; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .detail-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<header>
  <h1>NVR Tracking</h1>
  <p>Read-only monitoring dashboard · Latest saved reports</p>
</header>
<main>
  <div class="toolbar">
    <input id="search" type="search"
      placeholder="Search NVR ID, IP, model or camera name">
    <button id="refresh" type="button">Refresh reports</button>
    <span id="refresh-time"></span>
  </div>

  <p id="message" role="status">Loading reports…</p>
  <section id="stats" class="stats" aria-label="Monitoring summary"></section>
  <section id="nvr-list" aria-label="NVR reports"></section>
  <footer>
    Data is read from the latest saved JSON report for each NVR.
    This dashboard does not perform device configuration changes.
  </footer>
</main>

<script>
"use strict";

const byId = (id) => document.getElementById(id);
let latestNVRs = [];

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) {
    item.textContent = String(text);
  }
  return item;
}

function statusClass(value) {
  const raw = String(value || "UNKNOWN").toLowerCase();
  if (raw === "healthy" || raw === "warning" || raw === "critical") {
    return "pill " + raw;
  }
  if (raw === "unknown") return "pill unknown";
  return "pill";
}

function pill(value) {
  return element("span", statusClass(value), value || "UNKNOWN");
}

function formatSpace(value) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1000000) return (n / 1000000).toFixed(2) + " TB";
  if (n >= 1000) return (n / 1000).toFixed(1) + " GB";
  return n.toFixed(0) + " MB";
}

function table(headers, rows) {
  const wrap = element("div", "table-wrap");
  const tbl = element("table");
  const thead = element("thead");
  const headRow = element("tr");

  headers.forEach((header) => headRow.append(element("th", "", header)));
  thead.append(headRow);
  tbl.append(thead);

  const tbody = element("tbody");

  if (!rows.length) {
    const row = element("tr");
    const cell = element("td", "muted", "No entries reported");
    cell.colSpan = headers.length;
    row.append(cell);
    tbody.append(row);
  } else {
    rows.forEach((values) => {
      const row = element("tr");
      values.forEach((value) => row.append(element("td", "", value ?? "—")));
      tbody.append(row);
    });
  }

  tbl.append(tbody);
  wrap.append(tbl);
  return wrap;
}

function metric(label, value) {
  const box = element("div", "metric");
  box.append(element("div", "label", label));
  box.append(element("div", "value", value ?? "—"));
  return box;
}

function makeCard(nvr) {
  const card = element("article", "nvr-card");
  const head = element("div", "nvr-head");
  const titleBlock = element("div");
  titleBlock.append(element("h2", "", nvr.id || "Unknown NVR"));
  titleBlock.append(element(
    "div", "subline",
    `${nvr.ip || "IP unavailable"} · ${nvr.model || "Model not reported"} · ${nvr.firmware_version || "Firmware unavailable"}`
  ));
  head.append(titleBlock);
  head.append(pill(nvr.overall_status));
  card.append(head);

  const badges = element("div", "badges");
  badges.append(pill("Storage: " + (nvr.storage_status || "UNKNOWN")));
  badges.append(pill("Recording: " + (nvr.recording_status || "UNKNOWN")));
  card.append(badges);

  const metrics = element("div", "metrics");
  metrics.append(metric(
    "Channels",
    `${nvr.configured_channels ?? "—"} / ${nvr.expected_channels ?? "—"}`
  ));
  metrics.append(metric("Online", nvr.online_channels ?? 0));
  metrics.append(metric("Offline", nvr.offline_channels ?? 0));
  metrics.append(metric("Unknown", nvr.unknown_channels ?? 0));
  card.append(metrics);

  const lastCheck = nvr.last_check || "Not available";
  card.append(element("div", "muted", "Last check: " + lastCheck));

  const details = element("details");
  details.append(element(
    "summary", "",
    `Details — ${nvr.channels.length} channel records, ${nvr.disks.length} disk records`
  ));

  const grid = element("div", "detail-grid");

  const findingSection = element("section");
  findingSection.append(element("h3", "", "Health findings"));
  const findings = [
    ...nvr.critical_reasons.map((x) => ({label: "CRITICAL", text: x})),
    ...nvr.warnings.map((x) => ({label: "WARNING", text: x})),
    ...nvr.unknown_checks.map((x) => ({label: "UNVERIFIED", text: x}))
  ];

  if (!findings.length) {
    findingSection.append(element("p", "muted", "No findings reported."));
  } else {
    const list = element("ul", "findings");
    findings.forEach((item) => {
      list.append(element("li", "", item.label + ": " + item.text));
    });
    findingSection.append(list);
  }
  if (nvr.storage_message) {
    findingSection.append(element("p", "muted", "Storage: " + nvr.storage_message));
  }
  if (nvr.recording_message) {
    findingSection.append(element("p", "muted", "Recording: " + nvr.recording_message));
  }

  const diskSection = element("section");
  diskSection.append(element("h3", "", "Storage disks"));
  diskSection.append(table(
    ["ID", "Name / Type", "Status", "Capacity", "Free", "Property"],
    nvr.disks.map((d) => [
      d.id, `${d.name} / ${d.type}`, d.status,
      formatSpace(d.capacity_mb), formatSpace(d.free_space_mb), d.property
    ])
  ));

  const channelSection = element("section");
  channelSection.append(element("h3", "", "Offline / unknown channels"));
  const problemChannels = nvr.channels.filter(
    (ch) => String(ch.reported_status).toUpperCase() !== "ONLINE"
  );
  channelSection.append(table(
    ["Channel", "Camera", "IP", "Status"],
    problemChannels.map((ch) => [
      ch.channel_id, ch.name, ch.camera_ip, ch.reported_status
    ])
  ));

  const reportSection = element("section");
  reportSection.append(element("h3", "", "Report information"));
  reportSection.append(element("p", "muted", "File: " + (nvr.report_file || "—")));
  reportSection.append(element("p", "muted", "HTTP port: " + (nvr.http_port ?? "—")));
  reportSection.append(element("p", "muted", "Server port: " + (nvr.server_port ?? "—")));
  reportSection.append(element("p", "muted", "Recording track ID: " + (nvr.recording_track_id ?? "Not configured")));

  grid.append(findingSection, diskSection, channelSection, reportSection);
  details.append(grid);
  card.append(details);
  return card;
}

function renderStats(summary) {
  const stats = byId("stats");
  stats.replaceChildren();

  const items = [
    ["NVRs reported", summary.total_nvrs],
    ["Healthy", summary.healthy],
    ["Warnings", summary.warning],
    ["Critical", summary.critical],
    ["Cameras online", summary.cameras_online],
    ["Cameras offline", summary.cameras_offline]
  ];

  items.forEach(([label, value]) => {
    const box = element("div", "stat");
    box.append(element("div", "label", label));
    box.append(element("div", "value", value ?? 0));
    stats.append(box);
  });
}

function renderNVRs() {
  const query = byId("search").value.trim().toLowerCase();
  const list = byId("nvr-list");
  list.replaceChildren();

  const matches = latestNVRs.filter((nvr) => {
    const text = [
      nvr.id, nvr.ip, nvr.model,
      ...nvr.channels.map((c) => c.name || "")
    ].join(" ").toLowerCase();
    return text.includes(query);
  });

  if (!matches.length) {
    list.append(element("p", "muted", "No matching NVR reports found."));
    return;
  }
  matches.forEach((nvr) => list.append(makeCard(nvr)));
}

async function refresh() {
  const message = byId("message");
  message.textContent = "Loading latest reports…";
  message.className = "";

  try {
    const response = await fetch("/api/dashboard", {cache: "no-store"});
    if (!response.ok) throw new Error("Dashboard API returned HTTP " + response.status);

    const data = await response.json();
    latestNVRs = data.nvrs || [];
    renderStats(data.summary || {});
    renderNVRs();

    message.textContent = data.report_errors
      ? `Loaded reports with ${data.report_errors} unreadable file(s).`
      : "Showing the latest saved report for each NVR.";
    byId("refresh-time").textContent =
      "Page refreshed: " + new Date().toLocaleString();
  } catch (error) {
    message.textContent = "Could not load dashboard data: " + error.message;
    message.className = "error";
  }
}

byId("refresh").addEventListener("click", refresh);
byId("search").addEventListener("input", renderNVRs);
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


def _list_of_text(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _public_report(report: dict, path: Path) -> dict:
    """Expose only dashboard fields; never expose arbitrary report contents."""
    nvr = _as_dict(report.get("nvr"))
    device = _as_dict(report.get("device_info"))
    channel_inventory = _as_dict(report.get("channel_inventory"))
    channel_status = _as_dict(report.get("channel_status"))
    storage = _as_dict(report.get("storage"))
    recording = _as_dict(report.get("recording"))
    overall = _as_dict(report.get("overall"))

    channels = []
    raw_channels = channel_inventory.get("channels", [])
    if isinstance(raw_channels, list):
        for channel in raw_channels:
            channel = _as_dict(channel)
            channels.append({
                "channel_id": str(channel.get("channel_id", "")),
                "name": str(channel.get("name", "")),
                "camera_ip": str(channel.get("camera_ip", "")),
                "management_port": channel.get("management_port"),
                "reported_status": str(
                    channel.get("reported_status", "UNKNOWN")
                ),
            })

    disks = []
    raw_disks = storage.get("disks", [])
    if isinstance(raw_disks, list):
        for disk in raw_disks:
            disk = _as_dict(disk)
            disks.append({
                "id": str(disk.get("id", "")),
                "name": str(disk.get("name", "")),
                "type": str(disk.get("type", "")),
                "reported_status": str(
                    disk.get("reported_status", "UNKNOWN")
                ),
                "capacity_mb": disk.get("capacity_mb"),
                "free_space_mb": disk.get("free_space_mb"),
                "property": str(disk.get("property", "")),
                "group": str(disk.get("group", "")),
            })

    return {
        "id": str(nvr.get("id", path.name.split("_")[0])),
        "ip": str(nvr.get("ip", "")),
        "http_port": nvr.get("http_port"),
        "server_port": nvr.get("server_port"),
        "model": str(device.get("model", "")),
        "firmware_version": str(device.get("firmware_version", "")),
        "overall_status": str(
            _as_dict(overall).get("status", "UNKNOWN")
        ),
        "critical_reasons": _list_of_text(overall.get("critical_reasons")),
        "warnings": _list_of_text(overall.get("warnings")),
        "unknown_checks": _list_of_text(overall.get("unknown_checks")),
        "expected_channels": channel_inventory.get("expected_count"),
        "configured_channels": channel_inventory.get("configured_count"),
        "online_channels": channel_status.get("online_count", 0),
        "offline_channels": channel_status.get("offline_count", 0),
        "unknown_channels": channel_status.get("unknown_count", 0),
        "storage_status": str(storage.get("status", "UNKNOWN")),
        "storage_message": str(storage.get("message", "")),
        "recording_status": str(recording.get("status", "UNKNOWN")),
        "recording_message": str(recording.get("message", "")),
        "recording_track_id": recording.get("track_id"),
        "last_check": str(
            report.get("check_completed_at")
            or report.get("check_started_at")
            or ""
        ),
        "report_file": path.name,
        "channels": channels,
        "disks": disks,
    }


def read_dashboard_data() -> dict:
    """Read the newest JSON report for each NVR."""
    latest: dict[str, tuple[int, dict, Path]] = {}
    report_errors = 0

    if REPORTS_DIR.is_dir():
        for path in REPORTS_DIR.glob("nvr-*_*.json"):
            try:
                with path.open("r", encoding="utf-8") as file:
                    report = json.load(file)
                if not isinstance(report, dict):
                    report_errors += 1
                    continue

                nvr = _as_dict(report.get("nvr"))
                nvr_id = str(nvr.get("id", ""))
                if not nvr_id:
                    report_errors += 1
                    continue

                modified_ns = path.stat().st_mtime_ns
                previous = latest.get(nvr_id)
                if previous is None or modified_ns > previous[0]:
                    latest[nvr_id] = (modified_ns, report, path)

            except (OSError, json.JSONDecodeError):
                report_errors += 1

    nvrs = [
        _public_report(report, path)
        for _, report, path in latest.values()
    ]
    nvrs.sort(key=lambda item: item["id"])

    summary = {
        "total_nvrs": len(nvrs),
        "healthy": sum(nvr["overall_status"] == "HEALTHY" for nvr in nvrs),
        "warning": sum(nvr["overall_status"] == "WARNING" for nvr in nvrs),
        "critical": sum(nvr["overall_status"] == "CRITICAL" for nvr in nvrs),
        "cameras_online": sum(nvr["online_channels"] or 0 for nvr in nvrs),
        "cameras_offline": sum(nvr["offline_channels"] or 0 for nvr in nvrs),
        "cameras_unknown": sum(nvr["unknown_channels"] or 0 for nvr in nvrs),
    }

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "summary": summary,
        "nvrs": nvrs,
        "report_errors": report_errors,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "NVRDashboard"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        # Do not log request headers or credentials.
        super().log_message(fmt, *args)

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self) -> None:
        body = b"Authentication required.\n"
        self.send_response(401)
        self.send_header(
            "WWW-Authenticate",
            'Basic realm="NVR Tracking Dashboard", charset="UTF-8"',
        )
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, separator, token = header.partition(" ")
        if not separator or scheme.lower() != "basic":
            return False

        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False

        username, separator, password = decoded.partition(":")
        if not separator:
            return False

        username_ok = hmac.compare_digest(username, DASHBOARD_USERNAME)
        password_ok = hmac.compare_digest(password, DASHBOARD_PASSWORD)
        return username_ok and password_ok

    def do_GET(self) -> None:
        if not self._authorized():
            self._unauthorized()
            return

        path = urlsplit(self.path).path

        if path == "/":
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
            return

        if path == "/api/dashboard":
            try:
                body = json.dumps(
                    read_dashboard_data(),
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            except Exception:
                # Keep internal report details out of HTTP error responses.
                self._send(
                    500,
                    "application/json; charset=utf-8",
                    b'{"error":"Dashboard data could not be read."}',
                )
            return

        self._send(404, "text/plain; charset=utf-8", b"Not found.\n")


def main() -> int:
    if not DASHBOARD_USERNAME or not DASHBOARD_PASSWORD:
        raise SystemExit(
            "Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD in the environment."
        )

    if len(DASHBOARD_PASSWORD) < 16:
        raise SystemExit(
            "DASHBOARD_PASSWORD must be at least 16 characters long."
        )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)

    print(f"NVR dashboard listening on {HOST}:{PORT}")
    print(f"Reading reports from {REPORTS_DIR}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())