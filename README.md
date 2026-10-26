# NVR Track — Read-only CCTV recording monitoring

NVR Track monitors Hikvision NVRs using the existing Python ISAPI integration,
produces timestamped JSON reports, and serves an authenticated, responsive
**saved-report dashboard**. It does not query NVRs from the browser. The NVR
integration is **read-only** (GET endpoints plus POST for ISAPI recording
search only); no recording schedules, settings, media or storage are modified.

## Setup and safeguards

Python 3.12 and Docker Compose are used in production. Deploy under
`/opt/NVR_Track` on Ubuntu; the local inventory is
`config/nvrs.local.json`. The reporter and all-NVR runner need `.env` and
private inventory, while the dashboard only mounts `reports/` read-only.

- Back up production source and confirm `git status`, logs and local diffs
  before editing or applying patches; **never** discard uncommitted work.
- `.env`, private inventories and `reports/` are gitignored and excluded from
  Docker build context. Preserve `.env` file mode `600`; back it up into a
  root-only folder. Never print secrets or commit real IP inventories.
- Do not change unrelated services (including Traccar), networking, firewall,
  NVR settings or recording files.
- Dashboard Basic Authentication must remain configured. It is HTTP Basic;
  restrict access to the trusted management network or place it behind your
  existing HTTPS reverse proxy without changing other services.

## Recording feature flags

Add the following **keys and non-secret values** to the existing `.env`
without replacing any credentials. The server's `.env.example` was not included
in the inspection archive; review it separately before updating that tracked
example file:

| Setting | Example | Meaning |
|---|---|---|
| `NVR_PER_CAMERA_RECORDING_IDS` | `nvr-01` | Comma-separated NVR IDs to enable; others remain `NOT_CONFIGURED` |
| `NVR_RECORDING_WINDOW_MINUTES` | `60` | Lookback window, 1–1440 minutes; applies to NVR-wide and camera checks |
| `NVR_RECORDING_MAX_RESULTS` | `10` | Max returned segments per camera, 1–64 |
| `NVR_RECORDING_MAX_NVR_SECONDS` | `300` | Time budget per NVR for camera searches, 15–3600 seconds |
| `DASHBOARD_REFRESH_SECONDS` | `60` | Browser re-reads saved reports, 0 disables auto-refresh |

**Start with `nvr-01` only.** Expansion requires verifying the target NVR's
track listing in the report. Each enabled NVR calls
`GET /ISAPI/ContentMgmt/record/tracks`. Per-camera mapping uses the
`srcChannel` field when available (preferring main-stream `...01`); otherwise
`channel_id * 100 + 1` is accepted **only if that ID is present** in the
actual returned track list and there is no conflicting source-channel field.
A failed, inaccessible, ambiguous or invalid track-list response never
implies a track is absent. A missing track is distinct from a zero-result
recording search.

The report checks offline camera recording tracks too: live connectivity and
recording history are separate independent facts. Searches run **sequentially
per NVR**, with bounded request timeouts. On Linux, a shared
per-NVR advisory lock prevents overlapping report processes from searching the
same NVR. One NVR clock lookup is reused for
camera checks, and the verified timestamp treatment in `app/recording.py`
remains intact. Segment evidence is recognized by **overlap with the window**,
not by whether its start timestamp falls within the window. Recent segment
start/end timestamps, the check timestamp, verified track, count, and search
window are summarized in JSON instead of storing full search responses.

**A 60-minute window is a useful initial operational signal**, not proof of
continuous recording. Motion/event recording, schedules, delayed recording,
clock drift and retention gaps can make missing evidence expected. Consider a
longer window or schedule-aware future extension before alerting on
non-continuous cameras. Offline cameras with recent footage remain
`OFFLINE` + `RECORDING` independently.

### Per-camera recording statuses

| Status | Meaning |
|---|---|
| `RECORDING` | Verified matching segment overlaps the window on a verified track |
| `RECORDING_EVIDENCE_WITH_ANOMALY` | Evidence found, but segment timestamps or other results need review |
| `NO_RECENT_RECORDING_EVIDENCE` | Verified search returned no evidence **within the selected window**; not proof of no historical recordings |
| `TRACK_NOT_FOUND` | Verified track listing contains no usable track for this channel |
| `TRACK_MAPPING_UNVERIFIED` | Track listing inaccessible/invalid, or candidate mapping ambiguous |
| `CHECK_INCONCLUSIVE` | Device time or search result could not establish an answer |
| `CHECK_ERROR` | Permission, network, server or unexpected search failure |
| `NOT_CHECKED` | Not attempted (such as after time budget or systemic endpoint failure) |
| `NOT_CONFIGURED` | Camera checking not enabled for this NVR |

The report also retains the existing **NVR-wide** `recording` check. Its
success must **never** be counted as proof for every camera. Health priority:
critical HDD/device failures are critical; missing recording evidence and
camera offline findings are warnings; recording errors and unverifiable
checks are unknown findings. Old schema v1 reports remain readable; missing
per-camera results are **not** treated as recording.

### Check mappings on the remaining 11 NVRs (GET-only)

Before expanding `NVR_PER_CAMERA_RECORDING_IDS`, audit each NVR's actual
recording-track inventory. This command does not run recording searches:

```bash
cd /opt/NVR_Track
docker compose run --rm -T --entrypoint python nvr-report -m scripts.verify_recording_tracks --nvr-id nvr-01
# After examining the pilot, inspect the complete 12-NVR inventory:
docker compose run --rm -T --entrypoint python nvr-report -m scripts.verify_recording_tracks --all
```

This stores timestamped `reports/track-verification-*.json` with mapping
status for each camera and no credentials. Review any `TRACK_NOT_FOUND` or
`TRACK_MAPPING_UNVERIFIED` entries, and add an NVR ID to the enabled list
only when its main-stream track mapping is verified. A successful GET track
listing does not prove that the recording-search endpoint supports that track;
per-camera search failures remain explicitly classified when subsequently
enabled. Device-specific anomalies may require further read-only inspection.

## Reports, dashboard and operations

Run from the deployment directory:

```bash
cd /opt/NVR_Track
# One NVR (read-only live monitoring; will save a JSON report)
docker compose run --rm -T nvr-report --ip 10.233.220.51
# All configured NVRs, sequentially (uses same updated reporter)
docker compose run --rm -T nvr-report-all
# Build report and dashboard images
# Do this only after server changes are reconciled and validated.
docker compose build nvr-report nvr-report-all nvr-dashboard
# Replace only the dashboard container; do not restart unrelated services.
docker compose up -d --no-deps nvr-dashboard
# Check dashboard startup, latest reports and compose service status
docker compose ps
docker compose logs --tail=40 nvr-dashboard
```

Default dashboard is bound by `DASHBOARD_BIND_IP` (your existing
configuration is authoritative) on port `8080`. It re-reads the newest saved
JSON per NVR, based on `check_completed_at` and falls back to file modified
time. It never contacts NVRs. It preserves HTTP Basic Authentication,
whitelists API fields, uses `textContent` for device-supplied text, and
handles old/incomplete/bad reports. Report age over 26 hours shows as stale.

The dashboard supports NVR and camera search, NVR/storage/live/recording status
filters, offline cameras **with recordings**, missing recording evidence,
failed/unverified recording checks, urgency sorting and manual refresh.

## Tests (separate from production dependencies)

The project tests use Python `unittest` and `pytest`. Run either locally in an
isolated development virtual environment or with the **test-only** Docker
build target; production images do not include pytest or test files:

```bash
cd /opt/NVR_Track
python3 -m compileall -q app scripts
python3 -m unittest discover -s tests -v
git diff --check
# Isolated container test target; does not contact physical NVRs
docker build --target test -t nvr-track:test .
docker run --rm nvr-track:test
```

For a host test environment: `python3 -m venv .venv && .venv/bin/pip install
-r requirements.txt -r requirements-dev.txt && .venv/bin/python -m pytest -q`.
Do **not** add pytest to the runtime image to work around the earlier
`No module named pytest` message.

## Daily schedule and email

The existing production cron entry is reported to run at **08:00
Asia/Phnom_Penh**. The project does not install cron; inspect the existing
root/user crontabs and scripts before editing, and preserve them if already
correct. `nvr-report-all` runs the same `scripts/run_nvr_report.py` for each
NVR then builds the consolidated SMTP HTML+text email. An email is attempted
only when the expected `SMTP_*` keys are present. Review host, port,
`starttls`/`ssl`, FROM and recipient values without printing passwords.
A failed SMTP connection reports an exception **type**, not the server's
potentially sensitive exception text. Email delivery is not proven until a
scheduled run or an explicitly approved test to the configured recipient
succeeds. No emails are sent by the unit tests.

## Troubleshooting

- `TRACK_MAPPING_UNVERIFIED`: inspect read-only track endpoint credentials,
  permissions and XML schema; do not enable a guess-based mapping.
- `TRACK_NOT_FOUND`: compare channel ID and returned track IDs/
  `srcChannel`; zero search matches are **not** a missing track.
- `NO_RECENT_RECORDING_EVIDENCE`: check the configured lookback window,
  schedules and clock alignment before declaring a fault.
- `CHECK_INCONCLUSIVE`: inspect NVR clock and unexpected search response
  status; no evidence is claimed.
- `CHECK_ERROR`: check permissions, NVR reachability and request timeouts;
  no NVR configuration changes are needed.
- `NOT_CHECKED`: investigate per-NVR time budget or systemic endpoint failure.
- `CRITICAL` storage: investigate NVR-reported disk errors via the device
  administrator; **do not** format or change HDD settings from this app.
- Missing NVR cards: confirm inventory, per-NVR report generation, report
  directory mount and JSON syntax. Dashboard cannot invent missing reports.

## Source-of-truth reconciliation

The October 26 Ubuntu patch may be **newer** than the provided RAR/Git commit.
Before applying this package, inspect `git status --short --branch`,
`git diff --stat`, and the existing per-camera pilot code. Save uncommitted
work and backups. A patch included with a distribution may not apply cleanly
to that newer code; use `git apply --check` first and merge only reviewed,
compatible changes. Do not use `git reset --hard`, `git clean`, force-push,
or overwrite an uninspected server file. When server tests and live reports
pass, commit the validated changes on a review branch and push normally.
On Windows, fetch that new branch and inspect local changes before merge;
do not copy an older Windows directory over the Ubuntu version.
