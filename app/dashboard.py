"""Read-only, password-protected NVR monitoring dashboard."""

import base64
import hmac
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "reports"))
HOST = "0.0.0.0"
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


HTML = (Path(__file__).with_name("dashboard_ui.html")
        .read_text(encoding="utf-8"))


def _list_of_text(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _safe_count(value: object) -> int:
    try:
        if isinstance(value, bool):
            return 0
        n = int(value)
        return n if 0 <= n <= 100_000 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _refresh_seconds() -> int:
    try:
        return max(0, min(3600, int(os.environ.get("DASHBOARD_REFRESH_SECONDS", "60"))))
    except ValueError:
        return 60


EVIDENCE_STATUSES = {"RECORDING", "RECORDING_EVIDENCE_WITH_ANOMALY"}
UNVERIFIED_STATUSES = {
    "TRACK_NOT_FOUND", "TRACK_MAPPING_UNVERIFIED", "CHECK_INCONCLUSIVE",
    "CHECK_ERROR", "NOT_CHECKED", "NOT_CONFIGURED",
}
HISTORICAL_STATUSES = {
    "HISTORICAL_RECORDING_FOUND", "NO_HISTORICAL_EVIDENCE_IN_WINDOW",
    "HISTORICAL_CHECK_INCONCLUSIVE", "HISTORICAL_CHECK_ERROR",
    "HISTORICAL_NOT_CHECKED", "HISTORICAL_NOT_CONFIGURED",
}


def _public_recording(value: object) -> dict:
    """Normalize newer and pilot per-camera statuses; never leak unknown fields."""
    recording = _as_dict(value)
    legacy = {
        "RECORDING_EVIDENCE_FOUND": "RECORDING",
        "RECORDING_EVIDENCE_FOUND_WITH_TIME_ANOMALY": "RECORDING_EVIDENCE_WITH_ANOMALY",
        "NO_MATCHES_IN_WINDOW": "NO_RECENT_RECORDING_EVIDENCE",
        "SKIPPED_CAMERA_OFFLINE": "NOT_CHECKED",
        "NOT_CHECKED_TRACK_ID_NOT_CONFIGURED": "NOT_CONFIGURED",
    }

    raw = str(recording.get("status") or "NOT_CHECKED")
    status = legacy.get(raw, raw)
    if status not in (EVIDENCE_STATUSES | UNVERIFIED_STATUSES | {"NO_RECENT_RECORDING_EVIDENCE"}):
        status = "CHECK_INCONCLUSIVE"
    window = _as_dict(recording.get("requested_window_utc"))
    return {
        "status": status,
        "message": str(recording.get("message") or ("No per-camera recording result in this report." if not recording else ""))[:500],
        "track_id": recording.get("track_id"),
        "track_verified": bool(recording.get("track_verified")) if "track_verified" in recording else None,
        "num_matches": recording.get("num_matches"),
        "total_matches": recording.get("total_matches"),
        "most_recent_segment_end": recording.get("most_recent_segment_end") or recording.get("latest_segment_end"),
        "most_recent_segment_start": recording.get("most_recent_segment_start") or recording.get("latest_segment_start"),
        "checked_at": recording.get("checked_at") or recording.get("check_time"),
        "window_minutes": recording.get("window_minutes"),
        "requested_window_utc": {
            "start": window.get("start"), "end": window.get("end")
        },
        "anomalies": _list_of_text(recording.get("anomalies"))[:10],
    }


def _public_historical(value: object) -> dict:
    """Expose a small, trusted historical schema; legacy reports stay unverified."""
    raw = _as_dict(value)
    status = raw.get("status", "HISTORICAL_NOT_CHECKED")
    if not isinstance(status, str) or status not in HISTORICAL_STATUSES:
        status = "HISTORICAL_CHECK_INCONCLUSIVE"
    window = _as_dict(raw.get("requested_window_utc"))
    intervals = raw.get("intervals_examined")
    if not isinstance(intervals, list):
        intervals = []
    return {
        "status": status,
        "message": str(raw.get("message") or "Historical check not included in this report.")[:500],
        "track_id": raw.get("track_id"),
        "checked_at": raw.get("checked_at"),
        "num_matches": raw.get("num_matches"),
        "most_recent_segment_end": raw.get("most_recent_segment_end"),
        "most_recent_segment_start": raw.get("most_recent_segment_start"),
        "search_complete": raw.get("search_complete") is True,
        "from_cache": raw.get("from_cache") is True,
        "requested_window_utc": {"start": window.get("start"), "end": window.get("end")},
        "intervals_examined": [{
            "start": _as_dict(i).get("start"), "end": _as_dict(i).get("end"),
            "status": str(_as_dict(i).get("status", ""))[:80],
        } for i in intervals[:8]],
    }

def _public_camera_summary(value: object) -> dict:
    """Whitelist summary metadata; keep credentials and arbitrary JSON out of the API."""
    raw = _as_dict(value)
    return {
        "enabled": raw.get("enabled") is True,
        "window_minutes": _safe_count(raw.get("window_minutes")),
        "track_mapping_status": str(raw.get("track_mapping_status") or "UNVERIFIED")[:80],
        "nvr_clock_offset_seconds_approx": raw.get("nvr_clock_offset_seconds_approx")
        if type(raw.get("nvr_clock_offset_seconds_approx")) is int
        and -86400 <= raw["nvr_clock_offset_seconds_approx"] <= 86400 else None,
    }


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
            # Never elevate old reports to successful recording checks.
            per_camera = (channel.get("recording_result")
                          or channel.get("camera_recording")  # Existing Ubuntu pilot schema.
                          or channel.get("recording_check")
                          or channel.get("recording")
                          or ({"status": channel["recording_status"]}
                              if "recording_status" in channel else None))
            channels.append({
                "channel_id": str(channel.get("channel_id", "")),
                "name": str(channel.get("name", "")),
                "camera_ip": str(channel.get("camera_ip", "")),
                "management_port": channel.get("management_port"),
                "recording_result": _public_recording(per_camera),
                "historical_recording_result": _public_historical(
                    channel.get("historical_recording_result")
                ),
                "recording_expectation": (channel.get("recording_expectation")
                                          if isinstance(channel.get("recording_expectation"), str)
                                          and channel.get("recording_expectation") in {"continuous", "event", "unspecified"}
                                          else "unspecified"),
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
        "online_channels": _safe_count(channel_status.get("online_count")),
        "offline_channels": _safe_count(channel_status.get("offline_count")),
        "unknown_channels": _safe_count(channel_status.get("unknown_count")),
        "storage_status": str(storage.get("status", "UNKNOWN")),
        "storage_message": str(storage.get("message", "")),
        "recording_status": str(recording.get("status", "UNKNOWN")),
        "recording_message": str(recording.get("message", "")),
        "recording_track_id": recording.get("track_id"),
        "per_camera_recording": _public_camera_summary(
            report.get("per_camera_recording")
            or report.get("camera_recording_summary")
            or report.get("recording_summary")
        ),
        "last_check": str(
            report.get("check_completed_at")
            or report.get("check_started_at")
            or ""
        ),
        "report_file": path.name,
        "channels": channels,
        "disks": disks,
    }


def _is_fresh(timestamp: str) -> bool:
    try:
        checked = datetime.fromisoformat(timestamp)
        age = datetime.now(timezone.utc) - checked.astimezone(timezone.utc)
        return checked.tzinfo is not None and 0 <= age.total_seconds() < 26 * 3600
    except (ValueError, TypeError):
        return False


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
                recorded = report.get("check_completed_at") or report.get("check_started_at")
                try:
                    completed = datetime.fromisoformat(recorded)
                    if completed.tzinfo is None:
                        raise ValueError("Timezone missing")
                    ordering_ns = int(completed.timestamp() * 1_000_000_000)
                except (TypeError, ValueError, OverflowError):
                    ordering_ns = modified_ns
                previous = latest.get(nvr_id)
                if previous is None or ordering_ns > previous[0]:
                    latest[nvr_id] = (ordering_ns, report, path)

            except (OSError, json.JSONDecodeError):
                report_errors += 1

    nvrs = [
        _public_report(report, path)
        for _, report, path in latest.values()
    ]
    nvrs.sort(key=lambda item: item["id"])

    all_channels = [ch for nvr in nvrs for ch in nvr["channels"]]
    rec_statuses = [ch["recording_result"]["status"] for ch in all_channels]
    historical_statuses = [ch["historical_recording_result"]["status"] for ch in all_channels]
    summary = {
        "total_nvrs": len(nvrs),
        "healthy": sum(nvr["overall_status"] == "HEALTHY" for nvr in nvrs),
        "warning": sum(nvr["overall_status"] == "WARNING" for nvr in nvrs),
        "critical": sum(nvr["overall_status"] == "CRITICAL" for nvr in nvrs),
        "cameras_online": sum(nvr["online_channels"] or 0 for nvr in nvrs),
        "cameras_offline": sum(nvr["offline_channels"] or 0 for nvr in nvrs),
        "cameras_unknown": sum(nvr["unknown_channels"] or 0 for nvr in nvrs),
        "recording_evidence": sum(s in EVIDENCE_STATUSES for s in rec_statuses),
        "recording_no_evidence": rec_statuses.count("NO_RECENT_RECORDING_EVIDENCE"),
        "recording_unverified": sum(s in UNVERIFIED_STATUSES for s in rec_statuses),
        "recording_errors": rec_statuses.count("CHECK_ERROR"),
        "recording_not_checked": sum(s in {"NOT_CHECKED", "NOT_CONFIGURED"} for s in rec_statuses),
        "online_missing_expected": sum(
            ch["reported_status"] == "ONLINE" and ch["recording_expectation"] == "continuous"
            and ch["recording_result"]["status"] == "NO_RECENT_RECORDING_EVIDENCE"
            for ch in all_channels
        ),
        "historical_evidence": historical_statuses.count("HISTORICAL_RECORDING_FOUND"),
        "historical_no_evidence": historical_statuses.count("NO_HISTORICAL_EVIDENCE_IN_WINDOW"),
        "historical_unverified": sum(s not in {"HISTORICAL_RECORDING_FOUND", "NO_HISTORICAL_EVIDENCE_IN_WINDOW"} for s in historical_statuses),
        "historical_errors": historical_statuses.count("HISTORICAL_CHECK_ERROR"),
        # A deferred historical search is not a failed search. Expose the
        # operational backlog separately from the legacy aggregate field.
        "historical_pending_no_recent": sum(
            ch["recording_result"]["status"] == "NO_RECENT_RECORDING_EVIDENCE"
            and ch["historical_recording_result"]["status"] in {
                "HISTORICAL_NOT_CHECKED", "HISTORICAL_NOT_CONFIGURED",
            }
            for ch in all_channels
        ),
        "historical_inconclusive": historical_statuses.count("HISTORICAL_CHECK_INCONCLUSIVE"),
        "historical_skipped_recent": sum(
            ch["recording_result"]["status"] in EVIDENCE_STATUSES
            and ch["historical_recording_result"]["status"] == "HISTORICAL_NOT_CHECKED"
            for ch in all_channels
        ),
        "stale_reports": sum(
            not nvr["last_check"] or not _is_fresh(nvr["last_check"]) for nvr in nvrs
        ),
        "last_successful_report": max(
            (nvr["last_check"] for nvr in nvrs if nvr["last_check"] and
             nvr["overall_status"] in {"HEALTHY", "WARNING", "CRITICAL"}),
            default=None,
        ),
        "offline_with_evidence": sum(ch["reported_status"] == "OFFLINE"
                                     and (ch["recording_result"]["status"] in EVIDENCE_STATUSES
                                          or ch["historical_recording_result"]["status"] == "HISTORICAL_RECORDING_FOUND")
                                     for ch in all_channels),
        "storage_warnings": sum(nvr["storage_status"] == "WARNING" for nvr in nvrs),
        "storage_critical": sum(nvr["storage_status"] == "CRITICAL" for nvr in nvrs),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "refresh_seconds": _refresh_seconds(),
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