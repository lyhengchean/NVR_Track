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
**WARNING**, not proof of failure. When per-camera checks are enabled and
at least one camera on that NVR recorded within the search window, the NVR is
evidently overwriting old footage: the full disk is then only noted in
`storage.message` and storage can be **HEALTHY**. If no camera recorded, the
warning stays. Disk errors and missing expected disks are never relaxed.

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

Bays you have confirmed empty on site go in a separate list; their
`notexist` entries are then noted instead of raising a warning. A bay cannot
be in both lists, and an unlisted bay keeps warning:

```json
"expected_sata_hdd_ids": [1, 2, 4, 6],
"empty_sata_hdd_ids": [3, 5, 7]
```

No restart or rebuild is needed after editing `config/nvrs.local.json`; the
next report uses it.

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
status for each camera and no credentials. It ends by listing the fully
verified NVRs, the ones to keep disabled (and why), and the exact
`NVR_PER_CAMERA_RECORDING_IDS=` line that would add the fully verified ones
to what is enabled now. It never edits `.env`. Review any `TRACK_NOT_FOUND` or
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
handles old/incomplete/bad reports. A report older than the last scheduled
scan it should have been replaced by shows as stale (see *Stale reports*).

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

## One-step deploy and full check (Ubuntu)

```bash
cd /opt/NVR_Track && git pull --ff-only && sudo bash scripts/deploy_check.sh
```

Backs up the project (without `reports/`) to `/root`, pulls `main`, runs the
test image, rebuilds, recreates only `nvr-dashboard`, saves a fresh read-only
report for every NVR (no email), then checks the dashboard data, the 08:00
cron line, the last email result, `.env` permissions, Git ignores and the
server's packet loss. It ends with `RESULT: n PASS, n WARN, n FAIL` and saves
the log to `/root/nvr-track-check-<time>.txt`.

## Reading the dashboard

Each camera gets one **Condition**, built from results already in the report
(no extra NVR requests). The same verdict appears in the daily email.

| Condition | Meaning |
|---|---|
| No footage 30+ days | No video now and none on any day of the 30-day calendar. Camera is dead, disconnected or removed. |
| Stopped recording | No video in the search window, but older video exists (calendar or the `NVR_HISTORICAL_LOOKBACK_DAYS` history). |
| No recent events | An **online** camera marked `event` in `recording_expectations` had no segment in the search window. Normal for a quiet area, so it is not counted as stopped. An offline event camera, or one with no footage on any of the 30 calendar days, still shows as a problem. |
| Check failed | The recording check could not be completed (network, track or permission). Never counted as recording. |
| Gaps (N days) | Recording now, but N days since its oldest footage have no video. |
| Picture problem | Recording, but the latest snapshot is black, flat (covered), frozen or the view moved (needs snapshot checks). |
| Known issue | Someone marked the problem as being handled; alerts are paused until the date or until marked fixed. |
| Below required days (N days) | Recording now, but footage reaches back only N days, fewer than `NVR_REQUIRED_RETENTION_DAYS`. |
| Offline, still recording | NVR reports the camera offline, yet it recorded in the search window. |
| Short history (N days) | Recording now, but the NVR holds only N days (overwritten early, or camera added recently). |
| Not monitored | The NVR is not in `NVR_PER_CAMERA_RECORDING_IDS`, so no check ran. Never counted as recording or as a failure. |
| OK | Recording now with every calendar day covered. |

Layout, top to bottom:

- **Headline** answers the main question (how many cameras are not recording),
  followed by a bar of every camera's condition. Each legend entry filters the
  page to that condition; click it again to clear.
- **Fleet facts**: NVRs reporting and their health, live camera status,
  recording results for the search window, storage findings. Rare counts
  (calendars pending, failed history searches) appear only when above zero.
- **Filters**: search (NVR id, name, IP, model, firmware, camera name, channel,
  camera IP), NVR health, live status, condition, recording result, storage,
  **Problems only**, **All columns** (remembered per browser), sort, and
  **CSV** (downloads exactly the cameras in view).
- **NVR cards**: one coloured square per camera, the *camera wall*. Colour is
  the condition; stripes mean the camera is reported offline; dashed empty
  squares are expected cameras missing from the report because the NVR could
  not be reached. Filters dim the squares that do not match. Hover a square for
  the camera and reason; click it to open that camera's row.
- **Details** opens three tabs: *Cameras* (sortable; condition, live status,
  last video, recorded days with the 30-day strip, reason), *Storage* (disk
  state, capacity, free space, used %) and *Findings* (critical, warning and
  unverified checks, track list state, NVR clock offset, report file).

**Stale reports.** With automatic scans, an NVR's report is marked stale once
a later scheduled scan should have replaced it (two hours are allowed for a
fleet scan to reach each NVR). With scans every two hours, a stopped scanner is
flagged within about four hours instead of the next day. Without a known scan
schedule the old 26-hour rule applies.

"Last video" shows the end of the newest verified segment. When the NVR listed
only part of the footage and the newest segment could not be confirmed within
the time budget, it reads "*time* or later".

Keyboard: `/` focuses search, `Esc` clears it, `R` reloads reports. The theme
button cycles system, light and dark. The page is served with a strict
Content-Security-Policy; all device-provided text is rendered as plain text.

## NVR display names

Each NVR's `id` (`nvr-01` … `nvr-12`) is the technical key: it selects the
`NVR_01_USERNAME`/`NVR_01_PASSWORD` credentials, names the report files, links
each day's report to the previous one (calendar and history cache) and is used
in `NVR_PER_CAMERA_RECORDING_IDS` and `NVR_RETENTION_NVR_IDS`. **Do not change
it.** To show a real name, add an optional `name` (up to 80 characters) to the
entry in `config/nvrs.local.json`:

```json
{"id": "nvr-01", "name": "Kiln Building", "ip": "10.233.220.51", ...}
```

The dashboard then shows the name as the card title (with the id beside the
IP), search finds it, and the daily email lists `nvr-01 - Kiln Building`. It
takes effect from the next report; no rebuild is needed.

## Fill every camera column (Ubuntu)

```bash
cd /opt/NVR_Track && sudo bash scripts/fill_calendars.sh
```

Sets `NVR_RETENTION_MAX_CAMERAS_PER_RUN=64`, `NVR_RETENTION_CACHE_HOURS=20`,
`NVR_HISTORICAL_MAX_CAMERAS_PER_RUN=8` and
`NVR_DEFAULT_RECORDING_EXPECTATION=continuous` in `.env` (backup in `/root`),
rebuilds, then repeats read-only reports (no email) until no calendar is pending.
With 64 calendar checks per run and a 20-hour cache, every 08:00 run rescans
every camera's calendar, so each strip ends yesterday. With a longer cache a
calendar is reused for that long and the strip ends on an older day; the
dashboard then shows "Calendar up to <date>" under the count.

Empty-looking columns that are expected:
- *Historical recording: Not needed (recording now)* — a camera with recent
  recording evidence needs no history search.
- *Oldest day found / Oldest age* stay "—" for a camera with no recorded day
  in the 30-day calendar window.
- *Expectation* — `continuous` comes from the default; set `event` (or
  `unspecified`) for individual channels in `config/nvrs.local.json`
  under `recording_expectations`.

## Scan now and automatic scans

The `nvr-scanner` service runs every scan: the automatic ones and the ones
requested from the dashboard. It uses the same read-only reporters as
`nvr-report` and `nvr-report-all`, one scan at a time, and the same per-NVR
locks, so it never overlaps another run on the same NVR.

**Automatic scans** run at the times in `.env` (Cambodia time). The
consolidated email is sent only from the scans listed in `NVR_EMAIL_TIMES`:

```dotenv
NVR_SCAN_TIMES=06:00,08:00,10:00,12:00,14:00,16:00,18:00,20:00,22:00
NVR_EMAIL_TIMES=08:00
NVR_ON_DEMAND_SCANS=true
NVR_SCAN_COOLDOWN_SECONDS=60
```

A scan time missed because the service was restarting runs within 30 minutes;
older missed times are skipped, and a time never runs twice. A scan time that
falls due while another scan is still running (for example a manual "Scan all"
started at 07:45) runs as soon as that scan ends, however late. If that later
scan replaces an email time that was passed over, it still sends the email
once (likewise, an email time that is not also a scan time goes out with the
next scan). Without
`NVR_SCAN_TIMES` the service scans once at 08:00. After editing these lines:
`docker compose up -d --force-recreate nvr-scanner`.

**Scan now.** The dashboard's **Scan** button scans all NVRs or the NVR you pick
by name or ID, and every NVR card has its own **Scan** button. Use it after
adding or fixing a camera. Progress shows in the header ("Scanning nvr-04, 4 of
12"); the new reports load when it finishes. Manual scans never send email.
Only one scan runs at a time, and a new request waits
`NVR_SCAN_COOLDOWN_SECONDS` after the previous one. `NVR_ON_DEMAND_SCANS=false`
turns the button off.

How the request stays safe: the dashboard container has no NVR credentials.
It writes one small request file (`state/scan/request.json`, target `all` or an
NVR ID) after checking the login, a same-origin header and the JSON body. The
scanner accepts only IDs from `config/nvrs.local.json` and runs fixed commands;
nothing from the request reaches a shell. Its status file holds fixed codes,
times and NVR IDs only, never device output, IPs or credentials.

Logs: `docker compose logs -f nvr-scanner` (one line per scan start and finish).

### Moving from the 08:00 cron line to the scanner (once)

The scanner replaces the cron line. Keeping both would run two scans and send
two emails at 08:00. On the server:

```bash
cd /opt/NVR_Track
crontab -l > /root/crontab-backup-$(date +%Y%m%d-%H%M%S)
crontab -l | grep -v 'nvr-report-all' | crontab -
crontab -l | grep -c nvr-report-all            # expect 0
grep -l nvr-report-all /etc/cron.d/* 2>/dev/null  # expect nothing
```

The scanner's first start does not repeat a scan time that already passed
today, so starting it after 08:00 does not send a second email.

### Email

`nvr-report-all` runs the same `scripts/run_nvr_report.py` for each NVR, then
builds the consolidated SMTP HTML+text email; `--no-email` skips it. An email
is attempted only when the expected `SMTP_*` keys are present. A failed SMTP
connection reports an exception **type**, not the server's potentially
sensitive text. `deploy_check.sh` shows the last scheduled email result. No
emails are sent by the unit tests.

## Telegram bot

The `nvr-telegram` service lets allowed people check the system and receive
alerts in Telegram, on any phone or network. It connects **out** to
`api.telegram.org` (long polling), so no port, firewall rule or tunnel is
needed; the server only needs outbound HTTPS. It gets no NVR or SMTP
credentials (Compose passes it only `TELEGRAM_*` settings), never contacts an
NVR, reads the saved reports read-only through the same whitelisted view as
the dashboard, and asks for scans through `nvr-scanner` like the dashboard's
Scan button. Camera and NVR IP addresses are left out of messages unless
`TELEGRAM_SHOW_IPS=true`.

**Who can use it** is set by `TELEGRAM_ACCESS`. In every mode only
`TELEGRAM_ADMIN_USER_IDS` can start scans; everyone else gets the normal
read-only user features (status, problems, NVRs, cameras, offline, storage,
firmware, scanner, alerts on/off).

| `TELEGRAM_ACCESS` | Who gets user rights | IDs to collect |
|---|---|---|
| `allowlist` (default) | Only `TELEGRAM_ALLOWED_USER_IDS` and members of listed groups, inside those groups | One per person |
| `group` | Anyone who is a member of a group in `TELEGRAM_ALLOWED_CHAT_IDS` or `TELEGRAM_ALERT_CHAT_IDS`, also in a private chat with the bot. Checked with Telegram and re-checked every 10 minutes, so leaving the group removes access | None (only the group's ID) |
| `open` | Anyone on Telegram who finds the bot | None |

`open` also lets strangers who find the bot see which cameras are offline or
not recording; `group` gives the same "no IDs" convenience while you decide
who joins. In `open` and `group` mode IP addresses are shown to admins only
(and only with `TELEGRAM_SHOW_IPS=true`), non-admins are limited to 20
requests a minute, and `TELEGRAM_BLOCKED_USER_IDS` shuts out anyone. Refused
people get "This bot is private" with their own ID (once per 10 minutes);
unknown groups are ignored.

| Setting | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather. Secret: keep it only in `.env` |
| `TELEGRAM_ACCESS` | `allowlist`, `group` or `open` (above) |
| `TELEGRAM_ADMIN_USER_IDS` | People who may also start scans |
| `TELEGRAM_ALLOWED_USER_IDS` | Extra people with user rights (any mode) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Groups whose members have user rights |
| `TELEGRAM_ALERT_CHAT_IDS` | People or groups that get alerts and the daily summary (members have user rights) |
| `TELEGRAM_BLOCKED_USER_IDS` | People refused in every mode |
| `TELEGRAM_DIGEST_TIME` | Daily summary time, default `08:30`; empty turns it off |
| `TELEGRAM_KNOWN_REMINDER` | Weekly list of open known issues to the alert chats, default `mon 09:00`; `off` turns it off. No message when none are open |
| `NVR_KNOWN_REMINDER_DAYS` | Known issues open this many days are marked ⏳ in the reminder and *(review)* in the email (default 7) |
| `TELEGRAM_SHOW_IPS` | `true` to include IP addresses; default `false` |
| `NVR_CLOCK_DRIFT_WARN_SECONDS` | Alert when an NVR clock is off by more than this (default 120) |
| `TELEGRAM_HTTPS_PROXY` | Only if the server reaches the internet through a proxy |

**Set up (once).** Only your own ID is needed.
1. In Telegram, open **@BotFather**, send `/newbot`, choose a name and a
   username ending in `bot`, and copy the token. Optional: `/setdescription`,
   `/setuserpic`; `/setjoingroups` → Disable after step 4 if nobody should add
   the bot to other groups.
2. On the server: `cd /opt/NVR_Track && nano .env`, add
   `TELEGRAM_BOT_TOKEN=<token>` (`.env` keeps its 600 permissions).
3. `docker compose build nvr-telegram && docker compose up -d nvr-telegram`;
   `docker compose logs --tail 20 nvr-telegram` shows `running as @yourbot`.
4. Send `/myid` to the bot and put your number in `TELEGRAM_ADMIN_USER_IDS=`.
5. Choose who else may use it:
   - **Everyone, no IDs** — `TELEGRAM_ACCESS=open`. Share `https://t.me/yourbot`.
   - **Staff, no IDs** — create a Telegram group (e.g. "NVR Track"), add the
     bot and make it a group administrator (no extra permissions needed; this
     lets it check membership), send `/myid@yourbot` there, then set
     `TELEGRAM_ACCESS=group` and `TELEGRAM_ALERT_CHAT_IDS=<group id>` (the
     negative number). From then on, adding a person to the group gives them
     access and the group receives the alerts.
6. `docker compose up -d --force-recreate nvr-telegram`, then each person
   opens `https://t.me/yourbot` and taps **Start**.

**Commands** (also as a `/` menu and buttons): `/status`, `/problems`,
`/nvrs`, `/nvr nvr-01` (or `/nvr 1`, or its name), `/camera gate` (name,
`ch5`, camera IP, or `nvr-01 5`; in a private chat just type the name),
`/offline`, `/storage`, `/firmware`, `/scanner`, `/scan` (admins: buttons for
all or one NVR, confirmed before it starts; the bot reports back when the
scan ends), `/alerts on|off` (alerts in this chat), `/myid`, `/help`.

Add-on commands: `/known`, `/fixed`, `/note`, `/notes`, `/events`, `/uptime`, `/snapshot` (see *Add-ons* below).

**Alerts** are sent only when something **changes**, once per change, with a
matching "✅ resolved" message later: a camera stops recording or has no
footage for 30 days, a camera goes offline, an NVR cannot be scanned or turns
critical, storage becomes warning/critical, an NVR clock drifts, cameras fall
below the required days of footage, camera checks fail, reports go out of
date, or the scanner stops. A failed scan or failed check never produces a
false "recording again". On first start the bot posts one hello with the
current status instead of a flood of old problems. If Telegram cannot be
reached, the alert is retried. The daily summary doubles as a heartbeat: if it
stops arriving, the monitor itself is down.

## Required days of footage

`NVR_REQUIRED_RETENTION_DAYS=14` (for example) marks a recording camera whose
30-day calendar shows fewer days of footage as **Below required days**
(warning) on the dashboard, in the email and in Telegram, instead of the
informational *Short history*. It needs the recording calendar
(`NVR_RETENTION_ENABLED`) and a value no larger than
`NVR_RETENTION_WINDOW_DAYS`. `0` turns it off. After changing it:
`docker compose up -d --force-recreate nvr-dashboard nvr-scanner nvr-telegram`.

## Archiving old reports

About 100 reports are saved per day and none were ever removed. To move old
ones into compressed monthly archives in `reports/archive/`:

```bash
cd /opt/NVR_Track
# Dry run: lists what would be archived, changes nothing
docker compose run --rm -T --entrypoint python nvr-report -m scripts.archive_reports --older-than-days 60
# Archive them
docker compose run --rm -T --entrypoint python nvr-report -m scripts.archive_reports --older-than-days 60 --apply
```

Each archive is re-read and checked before the originals are removed. The
newest 200 reports of every NVR (at least 30) are always kept, so calendar and
history caches and the dashboard are unaffected. To run it automatically once a
day after a scheduled scan, set `NVR_REPORT_ARCHIVE_DAYS=60` and recreate
`nvr-scanner`. To look inside an archive:
`tar -tzf reports/archive/reports-2026-08-<run>.tar.gz | head`.

## Add-ons: known issues, repair log, changes, uptime, SMART, pictures, events

All add-ons stay read-only towards the NVRs (GET requests; the only POSTs
remain the existing recording searches and calendars).

**Known issues and repair log** (on by default). Mark a camera, or a whole
NVR, as a known issue: its alerts pause, it shows as *Known issue* on the
dashboard (instead of a problem), in the email under "Known issues (alerts
paused)" and in Telegram `/problems`. Alerts resume on the end date, or when
someone marks it fixed; if it is still broken then, the alert comes again.
Marking fixed also accepts the camera's current view as its reference
picture. Notes (who, when, what) are kept per camera.
- Dashboard: **Notes…** on a camera row, or *NVR known issue / note…* in Findings.
- Telegram: `/known` (list), `/known nvr-01 5 7d Cable cut` (admins; `1,3,12` for several cameras, `all`
  instead of 5 for the whole NVR; `3d`, `2w`, `2026-11-20` or `-` for until
  fixed), `/fixed nvr-01 5 spliced`, `/note nvr-01 5 Replaced cable`
  (admins, or everyone with `TELEGRAM_NOTES_BY=users`), `/notes nvr-01 [5]`.
  A single camera result from `/camera` has buttons for these.
- Reminders so known issues are not forgotten: every Monday 09:00
  (`TELEGRAM_KNOWN_REMINDER`) the alert chats get the open known issues with
  their age and what the latest scan says (⏳ open `NVR_KNOWN_REMINDER_DAYS`+
  days; ✅ the latest up-to-date scan found no problem, close it with `/fixed`).
  The daily email shows each known issue's age, marks old ones *(review)*, and
  lists known-issue cameras whose latest scan found no problem. `/fixed` also
  closes the known issue of a camera that is no longer in the NVR's list.
- Stored in `state/notes/notes.json` (back it up with the project).

**Camera list changes and NVR restarts** (on by default). Each report is
compared with the previous one whose channel list was retrieved: cameras
added, removed, renamed or with a new IP appear in Findings, the email,
the NVR warnings and as a one-off Telegram alert. `GET /ISAPI/System/status`
reads the NVR's uptime; a boot after the previous report is reported as a
restart. Turn the status read off with `NVR_DEVICE_STATUS=false`.

**Uptime report** (on by default). Online % and recording % per camera and
NVR, plus cameras that keep dropping on and off, from saved reports only.
Failed or disabled checks are left out, never counted either way.
- Telegram `/uptime` (last 30 days), `/uptime 7`, `/uptime 2026-10`, with a CSV file.
- On the 1st, after the first scheduled scan, last month's report is emailed
  (CSV attached) and posted to the alert chats. `NVR_UPTIME_MONTHLY=false` turns it off.
- By hand: `docker compose run --rm -T --entrypoint python nvr-report -m scripts.uptime_report --month 2026-10`
  (saves `reports/uptime/uptime-2026-10.csv`).

**Disk SMART, snapshot checks and real-time events** (off until enabled per
NVR). First check what the NVR supports (GET only, saves nothing):

```bash
cd /opt/NVR_Track
docker compose build nvr-report
docker compose run --rm -T --entrypoint python nvr-report -m scripts.probe_nvr_features --nvr-id nvr-01
```

It ends with the `.env` lines for what worked. Then:

| Add-on | Enable | What it does |
|---|---|---|
| Disk SMART | `NVR_SMART_NVR_IDS=nvr-01` | GET SMART status per installed SATA disk, once per `NVR_SMART_CACHE_HOURS` (24). Failing self-assessment, 100+ bad sectors or 60 °C+ is critical; any bad sectors or 55 °C+ is a warning. Shown in Storage (SMART, Temp.), NVR health, email, Telegram `/storage` and alerts. |
| Snapshot checks | `NVR_SNAPSHOT_NVR_IDS=nvr-01` | One picture per online camera per scan (within `NVR_SNAPSHOT_MAX_SECONDS`, 180). Black, flat/covered, frozen (identical to the previous scan) or moved gives *Picture problem* and an alert. Moved: the view differs from both the latest matching picture (which follows sun, shade and changing piles) and the matching picture from the same time of day, at `NVR_SNAPSHOT_MOVED_SCANS` scans in a row (default 2; the first shows *View re-check*, no alert). The picture from before the change is kept for comparison (dashboard **View**, Telegram `/snapshot`). The latest thumbnail per camera is kept in `state/snapshots` (`NVR_SNAPSHOT_KEEP=false` keeps none): dashboard **View**, Telegram `/snapshot nvr-01 5` for admins (`TELEGRAM_SNAPSHOTS=off` disables it). |
| Real-time events | `NVR_EVENT_NVR_IDS=nvr-01`, then `docker compose up -d nvr-events` | One alert-stream connection per NVR. Video loss, camera covered/tampered, disk error/full/unformatted, network cable, IP conflict, illegal login and video signal faults reach Telegram within seconds (repeats collapsed for `NVR_EVENT_REPEAT_MINUTES`, 30; known issues muted). Motion detection is ignored. `/events` and Findings show the last 24 h; a dropped stream is alerted. |

After changing these: `docker compose up -d --force-recreate nvr-scanner nvr-dashboard nvr-telegram`
(and `nvr-events` for events). Enable one NVR first, check a scan, then add more.

## Troubleshooting

- `TRACK_MAPPING_UNVERIFIED`: inspect read-only track endpoint credentials,
  permissions and XML schema; do not enable a guess-based mapping.
- `TRACK_NOT_FOUND`: compare channel ID and returned track IDs/
  `srcChannel`; zero search matches are **not** a missing track.
- `NO_RECENT_RECORDING_EVIDENCE`: check the configured lookback window,
  schedules and clock alignment before declaring a fault.
- `CHECK_INCONCLUSIVE`: inspect NVR clock and unexpected search response
  status; no evidence is claimed. A reply that fails validation is asked for
  once more after 3 seconds (often it was caught while a segment was opening);
  `retried_after_invalid_reply` marks it in the report. `0` in
  `NVR_RECORDING_RETRY_DELAY_SECONDS` disables this retry too.
- `CHECK_ERROR`: check permissions, NVR reachability and request timeouts;
  no NVR configuration changes are needed.
- `NOT_CHECKED`: investigate per-NVR time budget or systemic endpoint failure.
- Busy cameras (motion/event recording, or continuous recording split into
  many files): the NVR returns at most `NVR_RECORDING_MAX_RESULTS` segments,
  oldest first, with status `MORE`. After every camera has its primary
  result, the reporter uses the remaining time budget to locate the newest
  segment with a few extra read-only searches (at most 6 per camera for the
  recent window, 8 for a history interval). The recording status never
  depends on this step; it only corrects "Last video".
- A fleet run marks one NVR `PROCESS_TIMEOUT`: the runner allows
  `NVR_RECORDING_MAX_NVR_SECONDS` + 300 seconds per NVR (an invalid value
  falls back to 300 + 300). Raise the budget only if the NVR has many cameras.
- Dashboard speed: only the newest report file per NVR is read (an older one
  only if the newest is unreadable), so old reports in `reports/` do not slow
  it down. Old files can be archived manually; nothing deletes them.
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
last snapshot is otherwise recent. Individual single-NVR reports created
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
NVR_RETENTION_CACHE_HOURS=20
```

The NVR must also be included in the existing `NVR_PER_CAMERA_RECORDING_IDS`.
Rebuild the production image and run a single-NVR report to verify endpoint
support, then refresh the saved-report dashboard. Review only status codes,
calendar counts, and check timestamps (not raw XML, passwords or inventory).
If unsupported, the dashboard reports **Not supported** rather than zero days.
After a successful pilot, add more IDs to `NVR_RETENTION_NVR_IDS` and cautiously
increase `NVR_RETENTION_MAX_CAMERAS_PER_RUN` (maximum 64, default 2) while
observing execution duration and the existing per-NVR 300-second budget.
Every camera, online or offline, is eligible when it has a verified track and
clock. Cameras rotate fairly across daily reports, and a cache (default 20
hours, so each daily run refreshes it) prevents repeated requests for recently
checked cameras during manual runs. Stale scanned
results remain visible as **Previous scan**, never as current verified data.
The dashboard never queries devices directly.

The default report examines the last **30 completed device-local calendar days**;
the current partial day is excluded from them. Today is shown separately as an
extra outlined square at the end of the strip ("footage recorded today so far",
from the day's first calendar check); it never counts toward the 30 days or a
camera's condition. Supported range: 1–90 days. Each camera
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
