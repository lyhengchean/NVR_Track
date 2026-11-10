> Archived note. Paths and module names are from before the November 2026 layout change; see ../architecture.md.

# NVR_Track — VS Code file-by-file merge reference

Built from the 2026-10-26 Ubuntu source snapshot at Git HEAD 74fe6be, plus its TWO local edits to app/dashboard.py and scripts/run_nvr_report.py.

The files in this ZIP are the complete resulting versions of every file in the merge patch. **They are NOT a replacement for the entire Ubuntu repository.** Do not replace all project files wholesale.

IMPORTANT: check for newer Ubuntu edits before applying. Preserve the original .env (permission 600), private inventory, reports, cron, networking, and Traccar.

### Exact changed files

- UPDATE: `.dockerignore`
- UPDATE: `Dockerfile`
- UPDATE: `README.md`
- CREATE: `app/camera_recording.py`
- UPDATE: `app/dashboard.py`
- CREATE: `app/dashboard_ui.html`
- UPDATE: `app/recording.py`
- CREATE: `app/report_lock.py`
- UPDATE: `app/reporting.py`
- UPDATE: `compose.yaml`
- CREATE: `requirements-dev.txt`
- UPDATE: `scripts/run_all_nvr_reports.py`
- UPDATE: `scripts/run_nvr_report.py`
- CREATE: `scripts/verify_recording_tracks.py`
- CREATE: `tests/test_camera_recording.py`
- CREATE: `tests/test_dashboard_recording.py`
- CREATE: `tests/test_dashboard_server.py`
- CREATE: `tests/test_email_reporting.py`
- CREATE: `tests/test_report_lock.py`

### Preferred in VS Code (Remote SSH open /opt/NVR_Track)

1. Obtain NVR_Track_Ubuntu_merge.patch and move it to /home/lyheng on Ubuntu.
2. Open VS Code terminal for /opt/NVR_Track.
3. Verify `git status --short --branch`.
4. Check `git apply --check /home/lyheng/NVR_Track_Ubuntu_merge.patch`. Stop if check fails.
5. Make a restricted backup of current app, scripts, tests, Dockerfile, Compose, docs, and .env.
6. Only after checking compatibility and backup, run `git apply /home/lyheng/NVR_Track_Ubuntu_merge.patch`.
7. Review each change in Source Control, validate syntax and tests, and only then build/restart services.

### Alternative file-level comparison

Open the ZIP's changed paths in a SECOND VS Code window; use Select for Compare on the new file vs. the existing Ubuntu file. Merge consciously, especially for app/dashboard.py and scripts/run_nvr_report.py, which contain local pilot code. Do not copy .env from any package or invent track mappings.

### Recording settings to consider after code validation

NVR_PER_CAMERA_RECORDING_IDS=nvr-01
NVR_RECORDING_WINDOW_MINUTES=60
NVR_RECORDING_MAX_RESULTS=10
NVR_RECORDING_MAX_NVR_SECONDS=300
DASHBOARD_REFRESH_SECONDS=60

No NVR configuration writes or changes to recording schedules are included. 65 tests passed on a reconstructed local source copy; Ubuntu Docker, cameras, cron, and email were not verified.
