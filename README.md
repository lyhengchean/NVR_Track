# NVR Track

Read-only monitoring of Hikvision NVRs over ISAPI: which cameras are online,
which have recent recording evidence, 30-day recording calendars, disks and
SMART, camera pictures, changes, restarts, uptime and real-time events — on a
password-protected dashboard, in Telegram and by email. It never changes NVR
or camera settings, schedules, storage or video.

## Project layout

```
NVR_Track/
├── backend/                     Python 3.12, standard library HTTP server, no web framework
│   ├── nvr_track/
│   │   ├── core/                settings, NVR inventory, paths, locks, e-mail
│   │   ├── nvr/                 read-only ISAPI: device, storage, SMART, recording search,
│   │   │                        calendars, pictures, event stream
│   │   ├── monitoring/          camera conditions, live state, flapping, duplicates,
│   │   │                        camera changes, notes / known issues, uptime
│   │   ├── reports/             findings, monthly PDF, report builder
│   │   ├── web/                 dashboard server + JSON API (serves frontend/)
│   │   ├── bot/                 Telegram bot + message views
│   │   ├── services/            long-running: scanner, event listener, live status, healthcheck
│   │   └── cli/                 one-shot: reports, uptime, archive, probes
│   │       └── diagnostics/     manual one-NVR checks (ask for the password)
│   ├── tests/                   pytest suite; tests/fake_nvr.py simulates an NVR
│   ├── requirements.txt         pinned runtime packages
│   ├── requirements-dev.txt     + pytest
│   └── pyproject.toml           pytest settings
├── frontend/                    static pages, no build step
│   ├── dashboard/               index.html, dashboard.css, dashboard.js
│   └── report-builder/          index.html, report-builder.css, report-builder.js
├── deploy/Dockerfile            one image for every service (targets: runtime, test)
├── compose.yaml                 services, volumes, healthchecks
├── scripts/                     server scripts: deploy_check.sh, fill_calendars.sh
├── config/nvrs.example.json     inventory example
├── docs/                        operations, architecture, security
├── .github/workflows/ci.yml     tests on every pull request
└── .env.example                 settings example
```

On the server only, never in Git: `.env` (chmod 600), `config/nvrs.local.json`,
`reports/`, `state/`.

## Deploy (Ubuntu server)

```bash
cd /opt/NVR_Track && sudo bash scripts/deploy_check.sh
```

Backs up, pulls `main`, runs the test image, rebuilds, recreates the services,
checks every NVR and the dashboard, and ends with `RESULT: n PASS, n WARN, n FAIL`.

## Everyday commands (server, in `/opt/NVR_Track`)

| Task | Command |
|---|---|
| Status and logs | `docker compose ps` · `docker compose logs --tail 50 nvr-live nvr-events nvr-telegram nvr-scanner` |
| One NVR now | `docker compose run --rm -T nvr-report --ip <NVR IP>` |
| Monthly uptime + PDF | `docker compose run --rm -T --entrypoint python nvr-report -m nvr_track.cli.uptime_report --month 2026-11 --pdf` |
| Explain a recording search | `docker compose run --rm -T --entrypoint python nvr-report -m nvr_track.cli.explain_recording_search --nvr-id nvr-01 --channel 47` |
| Probe a feature on one NVR | `docker compose run --rm -T --entrypoint python nvr-report -m nvr_track.cli.probe_nvr_features --nvr-id nvr-01` |
| Archive old reports | `docker compose run --rm -T --entrypoint python nvr-report -m nvr_track.cli.archive_reports --older-than-days 60 [--apply]` |

Module names changed in November 2026 (`scripts.uptime_report` →
`nvr_track.cli.uptime_report`, …): [module map](docs/architecture.md#module-map-before-november-2026).

## Develop and test

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest -q backend/tests
```

On Windows use `.venv\Scripts\pip` and `.venv\Scripts\python`. To run a
service on your PC, start it from the repository root (it reads `config/`,
`reports/` and `state/` there):

```bash
PYTHONPATH=backend python -m nvr_track.web.dashboard        # PowerShell: $env:PYTHONPATH="backend"
```

Workflow: branch → tests → pull request (CI runs the tests and the Docker test
image) → merge → `deploy_check.sh` on the server.

## Rules

- NVRs stay read-only: GET requests, plus the ISAPI recording search and
  `dailyDistribution` queries. New device-facing features are opt-in per NVR,
  after a read-only probe on one NVR.
- Never print or commit `.env`, tokens, passwords or auth headers.
- A check that failed or could not run is never reported as success, and an
  empty search window is never "never recorded".

## Documentation

- [docs/operations.md](docs/operations.md) — setup, settings, deploy, scans,
  email, Telegram bot, add-ons, troubleshooting
- [docs/architecture.md](docs/architecture.md) — services, data flow, code map
- [docs/security.md](docs/security.md) — security review and hardening
- [docs/archive/](docs/archive/) — older reference notes
