"""CLI for a read-only recording-evidence check."""

import argparse
from getpass import getpass

from nvr_track.nvr.recording import check_recording_evidence, make_search_xml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", required=True, help="NVR IPv4 address")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--track-id", type=int, default=201)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--max-results", type=int, default=10)
    args = parser.parse_args()

    username = input("NVR username [admin]: ").strip() or "admin"
    password = getpass("Password (input hidden): ")

    if not password:
        print("Password cannot be empty.")
        return 2

    result = check_recording_evidence(
        ip=args.ip,
        username=username,
        password=password,
        port=args.port,
        track_id=args.track_id,
        minutes=args.minutes,
        max_results=args.max_results,
    )

    print(f"\nNVR: {args.ip}:{args.port}")
    print(f"Track ID: {args.track_id}")
    print(f"Status: {result['status']}")
    print(f"Message: {result['message']}")

    if result["http_status"] is not None:
        print(f"HTTP status: {result['http_status']}")

    window = result["requested_window_utc"]
    if window["start"] and window["end"]:
        print(f"Start UTC: {window['start']}")
        print(f"End UTC:   {window['end']}")

    print(f"Matches returned: {result['num_matches']}")
    print(
        "Total matches reported: "
        f"{result['total_matches'] if result['total_matches'] is not None else 'NOT_RETURNED'}"
    )

    for match in result["matches"]:
        print(
            f"Match: track={match['track_id']}"
            f" | start={match['start_utc']}"
            f" | end={match['end_utc']}"
        )

    if result["anomalies"]:
        print("Anomalies: " + ", ".join(result["anomalies"]))

    print("Playback URLs were not displayed.")

    return 0 if result["status"] in {
        "RECORDING_EVIDENCE_FOUND",
        "RECORDING_EVIDENCE_FOUND_WITH_TIME_ANOMALY",
        "NO_MATCHES_IN_WINDOW",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())