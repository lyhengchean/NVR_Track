# NVR_Track — system reference (state as of 05 Nov 2026, main = d852e8b, PR #22)

Read-only monitoring of 12 Hikvision NVRs (~330 cameras) over ISAPI. Everything runs with Docker Compose on Ubuntu host `nvr-tracking`, `/opt/NVR_Track`, time zone Asia/Phnom_Penh. Dashboard: `http://10.233.25.123:8080` (Basic auth; LAN only). Telegram bot: `@NVRTrackBot`. Repo: `lyhengchean/NVR_Track` (private), branch `main`, changes via pull requests (#5–#22 merged). Windows dev copy: `D:\Projects\hikvision-cctv-monitor`.

## Services (compose.yaml, image nvr-track:local)
| Service | Role | Secrets |
|---|---|---|
| nvr-scanner | Automatic scans 06:00–22:00 every 2 h (`NVR_SCAN_TIMES`), email only from the 08:00 scan (`NVR_EMAIL_TIMES`); runs dashboard/Telegram "Scan" requests (one at a time, cooldown); monthly uptime report on the 1st; optional daily report archiving | `.env` (NVR + SMTP) |
| nvr-dashboard | Web UI + JSON API from saved reports only; POST /api/scan, POST /api/notes, GET /api/snapshot | Dashboard login only |
| nvr-telegram | Bot: commands, buttons, change-based alerts, daily digest 08:30, real-time event posts, monthly uptime post | Telegram settings only (no NVR/SMTP) |
| nvr-events | Real-time alertStream listener per NVR in `NVR_EVENT_NVR_IDS` (idle when empty; not enabled yet) | `.env` |
| nvr-report / nvr-report-all | Manual one-shot reports | `.env` |

State dirs: `reports/` (JSON per NVR per scan, `fleet_latest_run.json`, `uptime/`, `archive/`), `state/scan`, `state/notes` (known issues + repair log), `state/events`, `state/snapshots` (latest thumbnail per camera + index), `state/telegram`. Private: `.env` (chmod 600), `config/nvrs.local.json`; both git-ignored, as is `reports/`.

## Code map
- `app/isapi.py` device info, channel inventory/status, device status (uptime). `app/storage.py` HDD list. `app/smart.py` SMART (rules version 2: only explicit failure words are critical; unknown words → UNVERIFIED).
- `app/recording.py` search engine (verified NVR-local timestamp handling, overlap rule, latest-segment refinement). `app/camera_recording.py` per-camera checks, track mapping (`channel*100+1` verified against `/ISAPI/ContentMgmt/record/tracks`), history + calendar orchestration, time budget. `app/historical_recording.py`, `app/retention.py` (30-day calendar via dailyDistribution).
- `app/camera_condition.py` one condition per camera. `app/reporting.py` NVR overall status. `app/inventory_changes.py` camera list changes + restart detection. `app/snapshots.py` picture checks. `app/events.py` event stream parser/files. `app/notes.py` known issues/repair log. `app/uptime.py` uptime stats. `app/mailer.py` SMTP.
- `app/dashboard.py` + `app/dashboard_ui.html` (vanilla JS, textContent only, CSP hashes). `app/telegram_views.py` messages + alert state.
- `scripts/run_nvr_report.py` (one NVR), `run_all_nvr_reports.py` (fleet + email), `scan_worker.py`, `telegram_bot.py`, `event_listener.py`, `verify_recording_tracks.py`, `probe_nvr_features.py`, `uptime_report.py`, `archive_reports.py`, `deploy_check.sh` (backup → pull → tests in test image → build/recreate → fresh report per NVR → dashboard data → scanner/email → secrets → network → Traccar untouched).
- Tests: `tests/` (281, pytest; `tests/fake_nvr.py` = fake Hikvision NVR for end-to-end tests). Docker test stage: `docker build --target test`.

## Camera conditions (dashboard, email, Telegram)
No footage 30+ days · Stopped recording · Check failed · Gaps · Below required days · Picture problem · Offline, still recording · Short history · Known issue · No recent events (event cameras) · Not monitored · OK. Recording statuses keep their own model (RECORDING, NO_RECENT_RECORDING_EVIDENCE, TRACK_NOT_FOUND, TRACK_MAPPING_UNVERIFIED, CHECK_INCONCLUSIVE, CHECK_ERROR, NOT_CHECKED, NOT_CONFIGURED…). No evidence in the window is never "never recorded"; failed/unverified checks are never counted as recording.

## Key settings (.env; see .env.example)
NVR access: `NVR_USERNAME`/`NVR_PASSWORD` or per-NVR `NVR_01_*`. Recording: `NVR_PER_CAMERA_RECORDING_IDS`, `NVR_RECORDING_WINDOW_MINUTES` (60), `NVR_RECORDING_MAX_NVR_SECONDS` (300), `NVR_HISTORICAL_*`, `NVR_RETENTION_*` (calendar), `NVR_DEFAULT_RECORDING_EXPECTATION`, `NVR_REQUIRED_RETENTION_DAYS` (0 = off). Schedule: `NVR_SCAN_TIMES`, `NVR_EMAIL_TIMES`, `NVR_ON_DEMAND_SCANS`, `NVR_SCAN_COOLDOWN_SECONDS`. Add-ons: `NVR_DEVICE_STATUS`, `NVR_SMART_NVR_IDS`, `NVR_SMART_CACHE_HOURS`, `NVR_SNAPSHOT_NVR_IDS`, `NVR_SNAPSHOT_MAX_SECONDS`, `NVR_SNAPSHOT_KEEP`, `NVR_EVENT_NVR_IDS`, `NVR_EVENT_REPEAT_MINUTES`, `NVR_EVENT_TYPES`, `NVR_UPTIME_MONTHLY`, `NVR_REPORT_ARCHIVE_DAYS`, `NVR_CLOCK_DRIFT_WARN_SECONDS`. SMTP: `SMTP_*`. Dashboard: `DASHBOARD_USERNAME/PASSWORD/BIND_IP`. Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ACCESS` (allowlist|group|open), `TELEGRAM_ADMIN_USER_IDS` (only admins scan/mark known issues/see pictures), `TELEGRAM_ALLOWED_USER_IDS`, `TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_ALERT_CHAT_IDS`, `TELEGRAM_BLOCKED_USER_IDS`, `TELEGRAM_DIGEST_TIME`, `TELEGRAM_SHOW_IPS`, `TELEGRAM_NOTES_BY`, `TELEGRAM_SNAPSHOTS`, `TELEGRAM_HTTPS_PROXY`. After editing `.env`: `docker compose up -d --force-recreate <service>`.

## Telegram commands
/status /problems /nvrs /nvr /camera (or type a name) /offline /storage /firmware /scanner /scan (admins, confirm step) /known [nvr ch|1,3,12|all until reason] /fixed /note /notes /events /uptime [days|YYYY-MM] /snapshot (admins) /alerts on|off /myid /help. Alerts only on change, with ✅ resolved follow-ups; failed checks never produce false recoveries; known issues mute silently; one-off events (camera list change, restart) announced once.

## Everyday commands (on the server)
```bash
cd /opt/NVR_Track
sudo bash scripts/deploy_check.sh                                   # after every merge
docker compose run --rm -T nvr-report --ip 10.233.220.51            # one NVR now
docker compose run --rm -T nvr-report-all --no-email                # all NVRs now
docker compose run --rm -T --entrypoint python nvr-report -m scripts.verify_recording_tracks --all
docker compose run --rm -T --entrypoint python nvr-report -m scripts.probe_nvr_features --nvr-id nvr-01
docker compose run --rm -T --entrypoint python nvr-report -m scripts.uptime_report --month 2026-10
docker compose run --rm -T --entrypoint python nvr-report -m scripts.archive_reports          # dry run
docker compose logs --tail 50 nvr-scanner nvr-telegram nvr-dashboard
```

## Observed fleet facts and open items (05 Nov 2026)
- All 12 NVRs report; per-camera checks on all; 0 failed/unverified checks; scheduled email SENT; Traccar untouched.
- 33 cameras with no footage for 30+ days (nvr-04: 15; nvr-01: ch1, 3, 12, 13, 21, 26, 51) — candidates for `/known … -` (until fixed).
- Really stopped (05 Nov): nvr-01 ch14 "361-BC3 RM Reject", ch42 "Workshop Welder 1" (both offline).
- nvr-01 ch21 and ch51 share camera IP 10.233.220.117; ch20 and ch22 both named "Cement Silo #1".
- Storage critical: nvr-01, nvr-04. 63 cameras keep < 30 days (set `NVR_REQUIRED_RETENTION_DAYS` if a policy exists).
- nvr-11 (DS-7716NI-E4) firmware V3.4.98 is very old.
- Real-time events not enabled yet; snapshots confirmed on nvr-01; enable add-ons per NVR after `probe_nvr_features`.
