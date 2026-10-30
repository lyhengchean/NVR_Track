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

### Storage alerts: detected disks versus undetected slots

Storage monitoring uses read-only `GET /ISAPI/ContentMgmt/Storage/hdd` responses.
Disk states `error`, `offline`, `smartfailed`, `unformatted`, and `mismatch`
are **CRITICAL**. `idle` is an accepted operational state (subject to its
capacity and write access); a writable disk with zero free space is a
**WARNING**, not proof of failure.

`notexist` means the NVR did not detect a disk at the reported entry; it
**does not prove that a drive was installed**. When other usable disks exist,
uncorroborated `notexist` entries are **WARNING** until the expected disk
inventory has been verified. If no operational disk is detected, the result
is **CRITICAL**. Unknown states remain **UNKNOWN** rather than healthy.

To identify missing drives that *should* be present, configure physical SATA
disk IDs only in the **private, gitignored** `config/nvrs.local.json`, for
example within a single NVR object:

```json
"expected_sata_hdd_ids": [1, 2]
```

**Do not copy these sample IDs into production as facts.** Only add IDs after
checking the device's installation records or observing the physical disk
inventory. A configured expected SATA ID absent from the device response or
reported `notexist` is **CRITICAL**. An absent virtual disk entry does not
satisfy or falsify a physical SATA expectation. Older inventory files without
this field remain valid. No storage device is modified by this check.

The overall health summary uses a generic urgent-storage message when a
critical finding is present; the saved `storage.message` and disk table give
the more precise diagnosis. Old reports are not rewritten, and the dashboard
always loads the newest saved report.


## Recording feature flags

Add the following **keys and non-secret values** to the existing `.env`
without replacing any credentials. The tracked `.env.example` contains placeholders only. Review it without
replacing your server's real `.env`:

| Setting | Example | Meaning |
|---|---|---|
| `NVR_PER_CAMERA_RECORDING_IDS` | `nvr-01` | Comma-separated NVR IDs to enable; others remain `NOT_CONFIGURED` |
| `NVR_RECORDING_WINDOW_MINUTES` | `60` | Lookback window, 1–1440 minutes; applies to NVR-wide and camera checks |
| `NVR_RECORDING_MAX_RESULTS` | `10` | Max returned segments per camera, 1–64 |
| `NVR_RECORDING_MAX_NVR_SECONDS` | `300` | Time budget per NVR for camera searches, 15–3600 seconds |
| `DASHBOARD_REFRESH_SECONDS` | `60` | Browser re-reads saved reports, 0 disables auto-refresh |
| `NVR_HISTORICAL_ENABLED` | `true` | Enable historical checks on NVRs with per-camera checks enabled |
| `NVR_HISTORICAL_LOOKBACK_DAYS` | `7` | Historical lookback (1–7 days), excluding the recent window |
| `NVR_HISTORICAL_MAX_CAMERAS_PER_RUN` | `2` | Upper bound of cameras receiving new historical queries per NVR report |
| `NVR_HISTORICAL_CACHE_HOURS` | `12` | Reuse a completed successful historical result for this many hours |

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
critical HDD/device failures are critical; missing expected **continuous** recording
evidence and camera offline findings are warnings; recording errors and unverifiable
checks are unknown findings. Old schema v1 reports remain readable; missing
per-camera results are **not** treated as recording.

### Recording expectations (continuous versus event)

Configure expectations in your **private** `config/nvrs.local.json` under the
appropriate NVR (no camera IPs or passwords belong in the tracked examples):

```json
"recording_expectations": {"12": "continuous", "13": "event"}
```

Allowed values are `continuous`, `event`, and `unspecified`. Unspecified is
the default. Only cameras marked `continuous` raise a missing-*expected*
recording warning after a verified no-match search. An event camera can
legitimately have no segment during a quiet 60-minute period. Its status
continues to show the evidence, but absence does not automatically alert.
For NVR-11, mark channels 12–16 `continuous` only if their actual recording
expectation is continuous. Live ONLINE/OFFLINE and recording evidence remain
independent.

### Separate historical checks

Historical status is saved as `channel_inventory.channels[].historical_recording_result`
next to each `recording_result` (recent). The historical field includes its own
verified track, timestamp, selected UTC window, examined intervals, completeness,
latest matching segment, and failure explanation.

| Historical status | Meaning |
|---|---|
| `HISTORICAL_RECORDING_FOUND` | At least one verified older segment overlaps a checked historical interval |
| `NO_HISTORICAL_EVIDENCE_IN_WINDOW` | All bounded intervals were checked successfully, without matching footage |
| `HISTORICAL_CHECK_INCONCLUSIVE` | Not all checked intervals could be verified or time budget expired mid-search |
| `HISTORICAL_CHECK_ERROR` | Endpoint, authorization, request, or unexpected search failure |
| `HISTORICAL_NOT_CHECKED` | Deferred by prioritization, budget, or unverifiable clock/track |
| `HISTORICAL_NOT_CONFIGURED` | Monitoring disabled |

Historical checks prioritize cameras with **no recent evidence**, including
OFFLINE cameras with verified tracks, and do not scan the full 330-camera
fleet automatically. At most two candidates per enabled NVR are selected by
default each run; each receives up to seven sequential one-day ISAPI queries.
A successful check can be reused from the latest saved report for up to 12
hours, clearly labeled with its original timestamp. When the cache has expired,
new historical searches prioritize cameras that have never received a check,
then the oldest previous checks. If the per-report limit defers a previously
checked camera, `last_attempted_at` preserves its most recent actual search
across saved reports while `checked_at` remains `null` (no new request ran).
This prevents old, expired cache entries from repeatedly taking slots ahead of
cameras that have never been investigated during 24-hour reporting cycles.
An inconclusive historical result is never treated as proof of missing footage;
the next eligible scheduled check can retry it.
The dashboard shows `Historical pending (no recent)` separately from the
legacy `historical_unverified` API count: skipping history for a camera with
recent evidence is an intentional optimization, not a monitoring error. Finding a segment stops
the search; therefore `search_complete=false` means *not all older intervals
were searched*, not that the found recording is unverified. No-result status
requires completion of every interval. Queries never modify recordings.

**Verify NVR-11 after enabling recording:** generate a fresh report, then
inspect only the five target channels using the saved JSON inspector:

```bash
cd /opt/NVR_Track
# Replace this example IP with the private address for nvr-11
# docker compose run --rm -T nvr-report --ip <NVR_11_IP>
docker compose run --rm -T --entrypoint python nvr-report \
  -m scripts.inspect_recording_report --nvr-id nvr-11 --channels 12,13,14,15,16
```

The inspector reports ONLINE state, track verification, recent status,
verified overlap, newest segment end, historical status, and the approximate
NVR-versus-system clock offset. It prints no camera names or camera IPs.
It exits 0 only if all five are ONLINE with verified recent segments. If a
camera still reports `NO_RECENT_RECORDING_EVIDENCE`, investigate its playback
recordings, timestamp differences, and expected schedule separately; do not
change NVR configuration from the monitoring system. A five-minute-plus clock
offset requires independent device/Ubuntu clock inspection, not an automatic
clock adjustment.

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
docker compose run --rm -T nvr-report --ip 192.0.2.51  # Replace with an address from your private inventory
# All configured NVRs, sequentially (uses same updated reporter)
docker compose run --rm -T -e SMTP_TO= nvr-report-all  # Do not email recipients during validation
# Build report and dashboard images
# Do this only after server changes are reconciled and validated.
docker compose build nvr-report  # All 3 app services share the same image tag
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

The desired production schedule is **08:00 Asia/Phnom_Penh**; this
archive cannot establish whether Ubuntu cron or systemd actually runs it. The project does not install cron; inspect the existing
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
- Missing footage on a normal full disk: zero free space is a warning requiring
  overwrite/quota review, **not proof of disk failure**. The current ISAPI
  HDD response does not by itself verify whether overwrite is enabled or
  whether an expected disk is missing; inspect those facts read-only.
- Brief NVR outages: a read that fails only because the NVR could not be
  reached (device info, channel list, live status, storage, track list, clock
  or one camera search) is repeated once after
  `NVR_RECORDING_RETRY_DELAY_SECONDS` (default 15, `0` disables). Login,
  permission and HTTP errors are never retried.
- Channel list timed out but live status worked: every channel is still
  reported from the status list (`channel_inventory.channel_source` =
  `CHANNEL_STATUS_FALLBACK`) and gets its recording check; names and IPs are
  blank and the inventory stays unverified.
- "Cameras with no result (NVR unreachable)" on the dashboard counts expected
  cameras that the latest report could not list. These are never counted as
  recording.
- Many NVRs timing out at once: test the server's own link first
  (`ping -c 100 -i 0.2 -q <gateway>` and `ip -s link show <interface>`). Packet
  loss to the gateway or growing RX drops is a host/VM network problem, not an
  NVR or code fault.

## Ubuntu verification checklist (without changing unrelated services)

Before pulling, inspect `git status --short --branch`, `git log -5 --oneline`,
`git diff --stat`, and confirm `.env` has permissions `600` with
`stat -c '%a %n' .env`. If the Ubuntu tree has local edits, review and back
them up before merging anything. In Windows VS Code, create a review branch,
copy only the reviewed CREATE/UPDATE files while preserving folder paths,
run unit tests and `git diff --check`, then commit and push the branch.
Review and merge on GitHub before using `git pull --ff-only` on Ubuntu.
Never replace the private `.env`, inventory, or reports from a file package.

After Ubuntu `git pull --ff-only`, build only the shared NVR application image:

```bash
cd /opt/NVR_Track
docker compose config --quiet
docker compose build nvr-report
docker build --target test -t nvr-track:test .
docker run --rm nvr-track:test
# Only the NVR dashboard is a continuously running service:
docker compose up -d --no-deps nvr-dashboard
docker compose ps nvr-dashboard
docker compose logs --tail=40 nvr-dashboard
```

Report commands are one-shot services. The server must have the real private
inventory and `.env` already in place; neither belongs in Git. To verify the
daily schedule without editing it, inspect `timedatectl`, `crontab -l`,
`sudo crontab -l`, and `systemctl list-timers --all`, and confirm the actual
08:00 entry and timezone. Run an all-NVR manual validation with
`-e SMTP_TO=` to suppress email during checks. Only test email delivery after
you have confirmed the intended recipient and explicitly approved sending.
No service, networking, firewall, or Traccar changes are necessary.

## Source-of-truth reconciliation

Your Ubuntu working tree may be **newer** than the uploaded ZIP commit.
Before applying this package, inspect `git status --short --branch`,
`git diff --stat`, and the existing per-camera pilot code. Save uncommitted
work and backups. A patch included with a distribution may not apply cleanly
to that newer code; use `git apply --check` first and merge only reviewed,
compatible changes. Do not use `git reset --hard`, `git clean`, force-push,
or overwrite an uninspected server file. When server tests and live reports
pass, commit the validated changes on a review branch and push normally.
On Windows, fetch that new branch and inspect local changes before merge;
do not copy an older Windows directory over the Ubuntu version.


## Latest fleet-run failure visibility

The all-NVR reporting runner writes `reports/fleet_latest_run.json` atomically
following each completed fleet run. It includes only NVR IDs and fixed success/error
codes. This file is stored under the existing reports directory and must **not**
be committed to Git. Existing JSON snapshots are never overwritten or deleted.

A failed NVR might not produce a new `nvr-XX_*.json` snapshot. The dashboard
compares the latest completed fleet-run manifest with saved report timestamps.
If the fleet run failed and no newer successful report exists, the NVR is marked
**Report failed**, the previous camera/storage details remain accessible with an
explicit **last successful report** warning, and old values are **excluded** from
fresh fleet totals. The stale/failed counter includes those devices, even if their
last snapshot is less than 26 hours old. Individual single-NVR reports created
afterward can clear a previous run failure for that NVR.

The consolidated email states how many NVRs produced reports and how many failed.
Camera counts in that email exclude execution-error rows. This distinction is
important when an NVR has an old successful snapshot but the current scheduled
report failed. If no `fleet_latest_run.json` exists (reports from an older
version), the dashboard remains compatible with the existing snapshot-only view.

A one-off `ERROR` in the email does not identify the reason for the process
failure. Check the relevant time window in `reports/scheduler.log` for safe,
fixed-code diagnostics, and investigate the NVR read-only before assuming a
disk or camera failure.


### Scheduled report lock-contention recovery

The all-NVR fleet runner runs each NVR sequentially and keeps the shared
per-NVR `flock` protection in place. If a child reporter exits with the exact
`Another report is already running ...; no queries sent.` lock-conflict
message, the fleet runner retries **at most twice**, after **5 seconds**
each. Each conflicted attempt exits **before any NVR request**. Healthy,
warning or critical reports, timeouts, invalid responses, credential errors,
and other failures are **not** retried.

If a lock is still busy after three total attempts, the fleet run records
`LOCK_CONFLICT` as an execution error in `fleet_latest_run.json` rather than
claiming zero cameras, and continues to the next NVR. The dashboard shows a
failed fleet run while preserving last-known camera details for inspection.
Do not remove `.locks` files or disable locking to bypass this protection.
A single root cron entry does not rule out overlapping manual runs; consult
cron journal and Docker process activity when investigating a collision.
This recovery is bounded and does not restart containers or touch the NVR
configuration.

## Per-camera recording calendar / footage-day evidence (opt-in)

This independent feature is separate from recent searches and the 7-day
historical troubleshooting rotation. The **read-only**
`POST /ISAPI/ContentMgmt/record/tracks/{verified_track_id}/dailyDistribution`
request asks for a monthly recording calendar on verified tracks. The request
body contains only `year` and `monthOfYear`; no recording control, downloads,
config updates, resets or storage operations occur. Each NVR is processed
sequentially under the existing per-NVR reporting lock.

**Off by default.** First verify support on one NVR (e.g. a previously
validated test device); add ONLY these lines to the existing private `.env`:

```dotenv
NVR_RETENTION_ENABLED=true
NVR_RETENTION_NVR_IDS=nvr-11
NVR_RETENTION_WINDOW_DAYS=30
NVR_RETENTION_MAX_CAMERAS_PER_RUN=2
NVR_RETENTION_CACHE_HOURS=168
```

The NVR must also be included in the existing `NVR_PER_CAMERA_RECORDING_IDS`.
Rebuild the production image and run a single-NVR report to verify endpoint
support, then refresh the saved-report dashboard. Review only status codes,
calendar counts, and check timestamps (not raw XML, passwords or inventory).
If unsupported, the dashboard reports **Not supported** rather than zero days.
After a successful pilot, add more IDs to `NVR_RETENTION_NVR_IDS` and cautiously
increase `NVR_RETENTION_MAX_CAMERAS_PER_RUN` (maximum 32, default 2) while
observing execution duration and the existing per-NVR 300-second budget.
Every camera, online or offline, is eligible when it has a verified track and
clock. Cameras rotate fairly across daily reports, and a cache (default 168
hours) prevents repeated requests for recently checked cameras. Stale scanned
results remain visible as **Previous scan**, never as current verified data.
The dashboard never queries devices directly.

The default report examines the last **30 completed device-local calendar days**;
the current partial day is excluded. Supported range: 1–90 days. Each camera
requires at most four month queries for a 90-day range. These checks do not
increase the budget of the separate historical search. Results stored per
camera in the JSON report: `retention_result`, including `status`, `as_of_date`,
`window_days`, `days_with_footage`, `days_examined`,
`oldest_verified_date`, `oldest_verified_age_days`, the compact daily calendar,
and check/cache metadata. Raw credentials, device responses, playback URLs and
video are never saved.

**Important evidence semantics:** A green day means the NVR explicitly reports
some video for that day; this does **not** prove the full day is recorded or
that the camera has continuously recorded since that date. The oldest verified
day/age is limited to the scanned window, **not the true oldest retained video**.
Orange means the device explicitly reported `record=false`, and gray means the
date could not be verified. Missing date entries never count as absent.
`RETENTION_VERIFIED` requires an explicit daily boolean for every date in the
window; `RETENTION_PARTIAL` means an incomplete calendar. `RETENTION_PENDING`
indicates budget/rotation deferral, `RETENTION_STALE` preserves an older audited
window, and error/unsupported/unverified outcomes never become zero-day claims.
Old JSON snapshots remain compatible and display `Not enabled` for this new
field. **Do not interpret `days_with_footage` as the number of continuous
recording days or exact storage retention.**
