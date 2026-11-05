"""Hikvision real-time event stream: parsing and the shared event files.

The nvr-events service holds one read-only GET /ISAPI/Event/notification/alertStream
connection per enabled NVR and appends the events that matter (video loss,
tampering, disk and network faults, illegal logins...) to state/events/events.jsonl.
The dashboard and the Telegram bot only read these files. Motion and smart
detections are ignored; repeats of the same event are collapsed.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_DOCUMENT = 64_000
MAX_BUFFER = 1_000_000
ROTATE_BYTES = 5_000_000
OPEN_TAG, CLOSE_TAG = b"<EventNotificationAlert", b"</EventNotificationAlert>"

# eventType (lower case): (label, severity, camera-level?)
EVENT_TYPES = {
    "videoloss": ("Video loss", "critical", True),
    "shelteralarm": ("Camera covered or tampered", "critical", True),
    "badvideo": ("Video signal abnormal", "warning", True),
    "videomismatch": ("Video standard mismatch", "warning", True),
    "recordexception": ("Recording exception", "critical", True),
    "recordingfailure": ("Recording failure", "critical", True),
    "diskfull": ("Disk full", "warning", False),
    "diskerror": ("Disk error", "critical", False),
    "hdderror": ("Disk error", "critical", False),
    "diskunformat": ("Disk not formatted", "critical", False),
    "nicbroken": ("NVR network disconnected", "critical", False),
    "ipconflict": ("IP address conflict", "warning", False),
    "illaccess": ("Illegal login attempt", "warning", False),
}


def events_dir() -> Path:
    return Path(os.environ.get("EVENTS_DIR", str(ROOT / "state" / "events")))


def wanted_types() -> dict:
    """NVR_EVENT_TYPES (comma list) limits which known types are kept."""
    chosen = {t.strip().lower() for t in os.environ.get("NVR_EVENT_TYPES", "").split(",") if t.strip()}
    return {k: v for k, v in EVENT_TYPES.items() if not chosen or k in chosen}


def _name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first(root: ET.Element, *names: str) -> str | None:
    wanted = {n.lower() for n in names}
    for node in root.iter():
        if _name(node.tag) in wanted and node.text and node.text.strip():
            return node.text.strip()
    return None


def parse_event(document: bytes) -> dict | None:
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        return None
    if _name(root.tag) != "eventnotificationalert":
        return None
    try:
        count = int(_first(root, "activePostCount") or 0)
    except ValueError:
        count = 0
    channel = _first(root, "channelID", "dynChannelID", "inputIOPortID") or ""
    return {
        "type": (_first(root, "eventType") or "").lower()[:40],
        "state": (_first(root, "eventState") or "").lower()[:20],
        "channel": channel if re.fullmatch(r"\d{1,4}", channel) else "",
        "device_time": (_first(root, "dateTime") or "")[:40],
        "count": count,
        "description": (_first(root, "eventDescription") or "")[:120],
    }


class StreamParser:
    """Feed raw bytes of the multipart stream; get parsed events back."""

    def __init__(self):
        self.buffer = b""

    def feed(self, chunk: bytes) -> list[dict]:
        self.buffer += chunk
        events = []
        while True:
            start = self.buffer.find(OPEN_TAG)
            if start < 0:
                self.buffer = self.buffer[-len(OPEN_TAG):]
                break
            end = self.buffer.find(CLOSE_TAG, start)
            if end < 0:
                self.buffer = self.buffer[start:]
                if len(self.buffer) > MAX_BUFFER:
                    self.buffer = b""  # Corrupt stream; resynchronise.
                break
            document = self.buffer[start:end + len(CLOSE_TAG)]
            self.buffer = self.buffer[end + len(CLOSE_TAG):]
            if len(document) <= MAX_DOCUMENT:
                event = parse_event(document)
                if event:
                    events.append(event)
        return events


def is_heartbeat(event: dict) -> bool:
    """Hikvision sends 'videoloss inactive' with count 0 as a keep-alive."""
    return event["type"] == "videoloss" and event["state"] == "inactive" and event["count"] == 0


class Deduplicator:
    """Keep the first 'active' event; drop repeats for repeat_minutes."""

    def __init__(self, repeat_minutes: int = 30):
        self.repeat = timedelta(minutes=repeat_minutes)
        self.last: dict[tuple, datetime] = {}

    def accept(self, nvr_id: str, event: dict, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if event["state"] not in {"active", ""}:
            return False
        key = (nvr_id, event["type"], event["channel"])
        previous = self.last.get(key)
        if previous is not None and now - previous < self.repeat:
            return False
        self.last[key] = now
        return True


def record(nvr_id: str, event: dict, types: dict, directory: Path | None = None) -> dict:
    """Append one event line (rotating the file when large)."""
    directory = directory or events_dir()
    directory.mkdir(parents=True, exist_ok=True)
    label, severity, camera_level = types[event["type"]]
    line = {"nvr": nvr_id, "type": event["type"], "label": label, "severity": severity,
            "channel": event["channel"] if camera_level else "",
            "at": datetime.now(timezone.utc).isoformat(), "device_time": event["device_time"],
            "description": event["description"]}
    path = directory / "events.jsonl"
    try:
        if path.stat().st_size > ROTATE_BYTES:
            os.replace(path, directory / "events.jsonl.1")
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line


def write_status(status: dict, directory: Path | None = None) -> None:
    directory = directory or events_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=".status",
                                     suffix=".tmp", delete=False) as handle:
        json.dump({"schema_version": 1, "updated_at": datetime.now(timezone.utc).isoformat(),
                   "nvrs": status}, handle)
        temporary = Path(handle.name)
    os.replace(temporary, directory / "status.json")


# ---------- readers (dashboard, Telegram) ----------

def read_status(directory: Path | None = None) -> dict:
    """{'updated_at', 'nvrs': {id: {...}}}; {} when the service never ran."""
    try:
        data = json.loads(((directory or events_dir()) / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("schema_version") == 1 else {}


def _valid(line: object) -> dict | None:
    if not isinstance(line, dict) or not isinstance(line.get("nvr"), str):
        return None
    if line.get("type") not in EVENT_TYPES:
        return None
    return {key: str(line.get(key) or "")[:120] for key in
            ("nvr", "type", "label", "severity", "channel", "at", "device_time", "description")}


def recent_events(hours: int = 24, limit: int = 500, directory: Path | None = None) -> list[dict]:
    """Newest first, within the last `hours`."""
    directory = directory or events_dir()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = []
    for name in ("events.jsonl.1", "events.jsonl"):
        try:
            text = (directory / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines()[-5000:]:
            try:
                line = _valid(json.loads(raw))
                if line and datetime.fromisoformat(line["at"]) >= cutoff:
                    rows.append(line)
            except (ValueError, TypeError):
                continue
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:limit]


def end_position(directory: Path | None = None) -> dict:
    """Position after the last complete line, so a reader starting now skips
    every older event (read_new() reads at most 2 MB per call)."""
    path = (directory or events_dir()) / "events.jsonl"
    try:
        stat = path.stat()
        start = max(0, stat.st_size - MAX_DOCUMENT)
        with path.open("rb") as handle:
            handle.seek(start)
            tail = handle.read(stat.st_size - start)  # Not past the size just read.
    except OSError:
        return {}
    cut = tail.rfind(b"\n")
    return {"inode": stat.st_ino, "offset": start + cut + 1 if cut >= 0 else start}


def read_new(position: dict, directory: Path | None = None) -> tuple[list[dict], dict]:
    """Lines appended since `position` ({'inode', 'offset'}); handles rotation."""
    path = (directory or events_dir()) / "events.jsonl"
    try:
        stat = path.stat()
    except OSError:
        return [], position
    offset = position.get("offset", 0) if position.get("inode") == stat.st_ino else 0
    if offset > stat.st_size:
        offset = 0
    rows = []
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(2_000_000)
    complete = data[:data.rfind(b"\n") + 1] if b"\n" in data else b""
    for raw in complete.decode("utf-8", errors="replace").splitlines():
        try:
            line = _valid(json.loads(raw))
        except ValueError:
            line = None
        if line:
            rows.append(line)
    return rows, {"inode": stat.st_ino, "offset": offset + len(complete)}
