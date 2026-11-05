"""Explain one camera's recording search: what the NVR replied and why
NVR_Track accepted or rejected it.

Read-only, the same requests as the scheduled check: GET /ISAPI/System/time,
then one POST /ISAPI/ContentMgmt/search for the last
NVR_RECORDING_WINDOW_MINUTES on the channel's main track. Prints the reply's
fields and every returned segment, marking the one that breaks a rule.
Never prints credentials.

  docker compose run --rm -T --entrypoint python nvr-report \
      -m scripts.explain_recording_search --nvr-id nvr-01 --channel 47
"""
from __future__ import annotations

import argparse
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException

from app.config import InventoryError, load_inventory
from app.recording import child_text, local_name, make_search_xml, parse_search_response, parse_search_timestamp

ROOT = Path(__file__).resolve().parent.parent


def setting(name: str, default: int, low: int, high: int) -> int:
    try:
        return min(max(int(os.environ.get(name, str(default))), low), high)
    except ValueError:
        return default


def describe(content: bytes, nvr_timezone) -> list[str]:
    """Readable lines for a search reply: header fields, then every segment."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [f"Reply is not XML; first bytes: {content[:120]!r}"]
    lines = [f"root={local_name(root.tag)} responseStatus={child_text(root, 'responseStatus')!r} "
             f"responseStatusStrg={child_text(root, 'responseStatusStrg')!r} "
             f"numOfMatches={child_text(root, 'numOfMatches')!r} totalMatches={child_text(root, 'totalMatches')!r}"]
    items = [node for node in root.iter() if local_name(node.tag) == "searchmatchitem"]
    lines.append(f"Segments in the reply: {len(items)}")
    for number, node in enumerate(items, 1):
        track, start, end = (child_text(node, "trackID"), child_text(node, "startTime"), child_text(node, "endTime"))
        problem = ""
        if not track or not start or not end:
            problem = "  <-- a field is missing"
        else:
            try:
                begins = parse_search_timestamp(start, nvr_timezone)
                ends = parse_search_timestamp(end, nvr_timezone)
                if ends <= begins:
                    problem = f"  <-- end is not after start ({(ends - begins).total_seconds():+.0f} s)"
                if not track.isdigit() or int(track) <= 0:
                    problem = "  <-- invalid track"
            except (ValueError, OverflowError) as exc:
                problem = f"  <-- {exc}"
        lines.append(f"  {number:>2}. track={track!r} start={start!r} end={end!r}{problem}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Explain one camera's recording search (read-only).")
    parser.add_argument("--nvr-id", required=True, help="e.g. nvr-01")
    parser.add_argument("--channel", required=True, type=int, help="camera channel, e.g. 47")
    parser.add_argument("--track", type=int, help="track ID (default: channel*100+1)")
    parser.add_argument("--minutes", type=int, default=setting("NVR_RECORDING_WINDOW_MINUTES", 60, 1, 1440))
    parser.add_argument("--max-results", type=int, default=setting("NVR_RECORDING_MAX_RESULTS", 10, 1, 64))
    args = parser.parse_args()
    try:
        nvr = next(n for n in load_inventory(ROOT / "config" / "nvrs.local.json") if n.id == args.nvr_id)
    except InventoryError as exc:
        print(f"Cannot load the inventory: {exc}")
        return 2
    except StopIteration:
        print("NVR ID is not in the inventory.")
        return 2
    suffix = nvr.id.upper().removeprefix("NVR-").replace("-", "_")
    username = os.environ.get(f"NVR_{suffix}_USERNAME", "").strip() or os.environ.get("NVR_USERNAME", "").strip()
    password = os.environ.get(f"NVR_{suffix}_PASSWORD", "") or os.environ.get("NVR_PASSWORD", "")
    if not username or not password:
        print("Credentials are not configured for this NVR.")
        return 2
    track = args.track or args.channel * 100 + 1
    minutes = min(max(args.minutes, 1), 1440)
    base, auth = f"http://{nvr.ip}:{nvr.http_port}", HTTPDigestAuth(username, password)
    try:
        with requests.Session() as session:
            session.trust_env = False
            clock = session.get(f"{base}/ISAPI/System/time", auth=auth, headers={"Accept": "application/xml"},
                                timeout=(3, 8), allow_redirects=False)
            if clock.status_code != 200:
                print(f"NVR clock: HTTP {clock.status_code}")
                return 1
            local = datetime.fromisoformat(child_text(ET.fromstring(clock.content), "localTime")
                                           .strip().replace("Z", "+00:00"))
            if local.tzinfo is None:
                print("NVR clock has no time zone offset.")
                return 1
            end = local.astimezone(timezone.utc).replace(microsecond=0)
            start = end - timedelta(minutes=minutes)
            print(f"{nvr.id} ch{args.channel} track {track}: NVR clock {local.isoformat()}; "
                  f"window {minutes} min; max results {args.max_results}")
            reply = session.post(f"{base}/ISAPI/ContentMgmt/search", auth=auth,
                                 data=make_search_xml(track, start, end, min(max(args.max_results, 1), 64),
                                                      request_timezone=local.tzinfo),
                                 headers={"Content-Type": "application/xml; charset=UTF-8",
                                          "Accept": "application/xml"},
                                 timeout=(3, 15), allow_redirects=False)
    except (RequestException, ET.ParseError, ValueError) as exc:
        print(f"Request failed: {type(exc).__name__}")
        return 1
    print(f"Search reply: HTTP {reply.status_code}")
    for line in describe(reply.content, local.tzinfo):
        print(line)
    if reply.status_code != 200:
        return 1
    try:
        parsed = parse_search_response(reply.content, timestamp_timezone=local.tzinfo,
                                       page_size=min(max(args.max_results, 1), 64))
    except ValueError as exc:
        print(f"Result: REJECTED by NVR_Track: {exc}")
        return 0
    total = f" of {parsed['total_matches']}" if parsed["total_matches"] not in (None, parsed["num_matches"]) else ""
    print(f"Result: accepted; {parsed['num_matches']}{total} segment(s), status {parsed['search_status'] or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
