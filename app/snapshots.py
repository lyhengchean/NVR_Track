"""Read-only snapshot checks: black, flat (covered), frozen or moved pictures.

One JPEG per online camera from GET /ISAPI/Streaming/channels/<ch>01/picture,
sequentially and within a time budget. Pictures are judged on a 64x48
greyscale copy; a small thumbnail is kept per camera in state/snapshots so
admins can see the latest picture (dashboard, Telegram /snapshot). Nothing is
sent anywhere by this module. Enable per NVR with NVR_SNAPSHOT_NVR_IDS after
scripts.probe_nvr_features shows pictures can be read.

Day and night (infrared, grey) pictures keep separate reference views, so the
nightly switch to IR is not taken for a moved camera. A view counts as the same
when it is close to the latest matching picture (which follows sun, shade and
changing material piles through the day) or to the matching picture taken at
the same time of day before (so morning light is compared with morning). A
picture that matches neither is re-checked: "view changed" only when it still
differs at NVR_SNAPSHOT_MOVED_SCANS scans in a row (default 2). The picture
from before the change is kept so people can compare. Marking a camera fixed
(Telegram /fixed or the dashboard) accepts its current view as the new
reference.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException

OK, BLACK, UNIFORM, FROZEN, MOVED = ("PICTURE_OK", "PICTURE_BLACK", "PICTURE_UNIFORM",
                                     "PICTURE_FROZEN", "PICTURE_VIEW_CHANGED")
# The view differs once: re-checked at the next scan before it counts as moved.
# Not a problem and no alert (alerts keep their previous state).
CHECKING = "PICTURE_VIEW_CHECKING"
UNAVAILABLE, NOT_CHECKED, NOT_CONFIGURED = "PICTURE_UNAVAILABLE", "PICTURE_NOT_CHECKED", "PICTURE_NOT_CONFIGURED"
STATUSES = {OK, BLACK, UNIFORM, FROZEN, MOVED, CHECKING, UNAVAILABLE, NOT_CHECKED, NOT_CONFIGURED}
BLACK_BELOW = 10          # Mean brightness 0-255.
UNIFORM_BELOW = 3.5       # Standard deviation of brightness.
MOVED_ABOVE = 24          # Differing bits of 64 in the difference hash.
GREY_BELOW = 3.0          # Mean colour difference: IR night pictures are grey.
BAND_HOURS = 2            # Same-time-of-day references: one per scan slot.
MAX_BYTES = 5_000_000
THUMB_SIZE = (640, 360)
LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")


def moved_scans_needed() -> int:
    """Scans in a row a view must differ before it is "view changed" (1-6)."""
    try:
        return min(max(int(os.environ.get("NVR_SNAPSHOT_MOVED_SCANS", "2")), 1), 6)
    except ValueError:
        return 2


def result(status: str, message: str, **extra) -> dict:
    return {"status": status, "message": message, "checked_at": datetime.now(timezone.utc).isoformat(),
            "brightness": None, "contrast": None, "mode": None, "distance": None,
            "has_image": False, "has_reference": False, **extra}


def stream_id(channel_id: str, track_id: object) -> int | None:
    """Main stream of the channel: the verified recording track if known (101 for ch1)."""
    if isinstance(track_id, int) and track_id > 0 and track_id % 100 == 1:
        return track_id
    return int(channel_id) * 100 + 1 if str(channel_id).isdigit() and int(channel_id) > 0 else None


def analyze(jpeg: bytes) -> dict:
    """Brightness, contrast, day/night mode and a 64-bit difference hash."""
    from PIL import Image, ImageStat  # Imported here: only needed when enabled.
    with Image.open(io.BytesIO(jpeg)) as image:
        image.load()
        rgb = image.convert("RGB")
    small = rgb.resize((64, 48))
    grey = small.convert("L")
    stat = ImageStat.Stat(grey)
    pixels = list(small.getdata())
    colour = sum(abs(p[0] - p[1]) + abs(p[1] - p[2]) + abs(p[0] - p[2]) for p in pixels) / (3 * len(pixels))
    tiny = rgb.convert("L").resize((9, 8))
    values = list(tiny.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (values[row * 9 + col] > values[row * 9 + col + 1])
    return {"brightness": round(stat.mean[0], 1), "contrast": round(stat.stddev[0], 1),
            "mode": "night" if colour < GREY_BELOW else "day", "dhash": f"{bits:016x}",
            "width": rgb.width, "height": rgb.height}


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _band_key(mode: str, when: datetime) -> str:
    return f"{mode}@{when.astimezone(LOCAL_TZ).hour // BAND_HOURS * BAND_HOURS:02d}"


def _drop_refs_before(refs: dict, reset_after: str | None) -> bool:
    """Marked fixed: forget every reference saved before that moment."""
    if not reset_after:
        return False
    try:
        cutoff = datetime.fromisoformat(reset_after)
    except (TypeError, ValueError):
        return False
    dropped = False
    for key in list(refs):
        try:
            older = datetime.fromisoformat(refs[key]["at"]) < cutoff
        except (KeyError, TypeError, ValueError):
            older = True
        if older:
            del refs[key]
            dropped = True
    return dropped


def judge(metrics: dict, sha: str, previous: dict | None, reset_after: str | None,
          now: datetime | None = None) -> tuple[str, str, dict]:
    """(status, message, updated index entry) for one picture.

    References per mode: "day"/"night" (latest matching picture) and
    "day@14" etc. (latest matching picture in that time-of-day slot). The view
    is the same when it is close to either; then both are updated, so they
    follow light and scene changes. Otherwise the camera is re-checked and is
    "view changed" only after moved_scans_needed() scans in a row.
    """
    previous = previous if isinstance(previous, dict) else {}
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    refs = {key: value for key, value in (previous.get("refs") or {}).items() if isinstance(value, dict)}
    was_reset = _drop_refs_before(refs, reset_after)
    # Scans in a row the view has differed. Index entries written before this
    # rule kept no count: one that was "view changed" stays so (no alert flap).
    count = previous.get("moved_scans")
    if not isinstance(count, int):
        count = moved_scans_needed() if (previous.get("distance") or 0) > MOVED_ABOVE else 0
    if was_reset:
        count = 0
    mode = metrics["mode"]
    band = _band_key(mode, now)
    entry = {"sha": sha, "dhash": metrics["dhash"], "checked_at": stamp, "refs": refs, "moved_scans": count}
    if metrics["brightness"] < BLACK_BELOW:
        return BLACK, f"Black picture (brightness {metrics['brightness']}).", entry
    if metrics["contrast"] < UNIFORM_BELOW:
        return UNIFORM, f"Flat picture with no detail (contrast {metrics['contrast']}).", entry
    try:
        recent = now - datetime.fromisoformat(previous["checked_at"]) < timedelta(hours=26)
    except (KeyError, TypeError, ValueError):
        recent = False
    if previous.get("sha") == sha and recent:
        return FROZEN, "Exactly the same picture as at the previous scan (frozen image).", entry
    known = [refs[key] for key in (mode, band) if key in refs]
    current = {"dhash": metrics["dhash"], "at": stamp}
    if not known:
        refs[mode] = refs[band] = current
        entry["moved_scans"] = 0
        return OK, f"Picture OK ({mode}); saved as the reference view.", entry
    distance = min(hamming(metrics["dhash"], ref["dhash"]) for ref in known)
    entry["distance"] = distance
    if distance <= MOVED_ABOVE:
        refs[mode] = refs[band] = current  # Follow light, weather and scene changes.
        entry["moved_scans"] = 0
        return OK, f"Picture OK ({mode}).", entry
    entry["moved_scans"] = count + 1
    if count + 1 >= moved_scans_needed():
        return MOVED, (f"View still differs from the {mode} reference ({distance}/64) at the latest scans. "
                       "If the camera was re-aimed on purpose, mark it fixed to accept the new view."), entry
    return CHECKING, (f"View differs from the {mode} reference ({distance}/64); checked again at the "
                      "next scan before it counts as moved."), entry


def fetch(session: requests.Session, base: str, auth, stream: int) -> tuple[str, bytes | None]:
    try:
        with session.get(f"{base}/ISAPI/Streaming/channels/{stream}/picture", auth=auth,
                         timeout=(3, 15), allow_redirects=False, stream=True) as response:
            if response.status_code in (401, 403):
                return "PERMISSION_DENIED", None
            if response.status_code != 200:
                return f"HTTP_{response.status_code}", None
            body = b""
            for chunk in response.iter_content(65536):
                body += chunk
                if len(body) > MAX_BYTES:
                    return "TOO_LARGE", None
    except RequestException:
        return "CONNECTION_ERROR", None
    if not body.startswith(b"\xff\xd8"):
        return "NOT_A_JPEG", None
    return "OK", body


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix="." + path.name,
                                     suffix=".tmp", delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def thumbnail(jpeg: bytes) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(jpeg)) as image:
        image = image.convert("RGB")
        image.thumbnail(THUMB_SIZE)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=70)
    return out.getvalue()


def check_snapshots(*, ip: str, port: int, username: str, password: str, nvr_id: str,
                    channels: list[dict], state_dir: Path, deadline: float,
                    resets: dict[str, str] | None = None, keep_images: bool = True,
                    fetcher=fetch) -> dict:
    """Set channel['snapshot_result'] for every channel; return counts."""
    folder = state_dir / nvr_id
    try:
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
        index = index if isinstance(index, dict) else {}
    except (OSError, ValueError):
        index = {}
    resets = resets or {}
    base = f"http://{ip}:{port}"
    auth = HTTPDigestAuth(username, password)
    with requests.Session() as session:
        session.trust_env = False
        for ch in channels:
            cid = str(ch.get("channel_id", ""))
            stream = stream_id(cid, (ch.get("recording_result") or {}).get("track_id"))
            if ch.get("reported_status") != "ONLINE":
                ch["snapshot_result"] = result(NOT_CHECKED, "Camera not online.")
                continue
            if stream is None:
                ch["snapshot_result"] = result(NOT_CHECKED, "No stream for this channel.")
                continue
            if time.monotonic() >= deadline:
                ch["snapshot_result"] = result(NOT_CHECKED, "Snapshot time budget reached.")
                continue
            code, jpeg = fetcher(session, base, auth, stream)
            if code != "OK" or jpeg is None:
                ch["snapshot_result"] = result(UNAVAILABLE, f"Picture not available ({code}).")
                continue
            try:
                metrics = analyze(jpeg)
            except Exception:  # Corrupt JPEG or missing image library.
                ch["snapshot_result"] = result(UNAVAILABLE, "Picture could not be decoded.")
                continue
            sha = hashlib.sha256(jpeg).hexdigest()
            previous = index.get(cid) if isinstance(index.get(cid), dict) else {}
            status, message, entry = judge(metrics, sha, previous, resets.get(cid))
            index[cid] = entry
            differs = status in {CHECKING, MOVED}
            stored, has_reference = False, False
            if keep_images:
                latest, before = folder / f"{cid}.jpg", folder / f"{cid}.ref.jpg"
                try:
                    if differs and entry["moved_scans"] == 1 and latest.is_file():
                        # Keep the picture from before the change, to compare with.
                        _write_atomic(before, latest.read_bytes())
                        entry["reference_at"] = previous.get("checked_at")
                    elif differs:
                        entry["reference_at"] = previous.get("reference_at")
                    elif before.exists() and status == OK:
                        before.unlink()
                    has_reference = differs and before.is_file()
                except OSError:
                    has_reference = False
                try:
                    _write_atomic(latest, thumbnail(jpeg))
                    stored = True
                except Exception:
                    stored = False
            ch["snapshot_result"] = result(status, message, brightness=metrics["brightness"],
                                           contrast=metrics["contrast"], mode=metrics["mode"],
                                           distance=entry.get("distance"), has_image=stored,
                                           has_reference=has_reference, checked_at=entry["checked_at"],
                                           reference_at=entry.get("reference_at") if has_reference else None)
    try:
        _write_atomic(folder / "index.json", json.dumps(index).encode("utf-8"))
    except OSError:
        pass
    statuses = [(ch.get("snapshot_result") or {}).get("status") for ch in channels]
    return {"enabled": True, **{s: statuses.count(s) for s in sorted(STATUSES) if statuses.count(s)}}


def not_configured(channels: list[dict]) -> dict:
    for ch in channels:
        ch["snapshot_result"] = result(NOT_CONFIGURED, "Snapshot checks are not enabled for this NVR.")
    return {"enabled": False}
