# Security

Review of `main` @ 58639ac (10 Nov 2026): Telegram bot, dashboard and API,
secrets, NVR access, containers, deploy script, dependencies and the full Git
history (120 commits, all branches).

## Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | High if `TELEGRAM_ACCESS=open` | Anyone on Telegram can find the bot and read offline / not-recording cameras, camera names, NVR models and firmware, events and notes, and subscribe a chat to alerts | **Server setting**: use `group` or `allowlist` |
| 2 | Medium | Any member of an alert group could send `/alerts off` and silence the group | Fixed (PR #46): admins only in groups |
| 3 | Medium | No socket timeout: idle connections held dashboard threads forever, before any login | Fixed (PR #46): 30 s |
| 4 | Medium | Unlimited dashboard password guesses | Fixed (PR #46): 10 different wrong logins in 10 min lock the address for 10 min |
| 5 | Medium | Dashboard is plain HTTP with Basic auth: the password crosses the LAN in clear text with every refresh | Available: `DASHBOARD_TLS` (opt-in; see docs/operations.md "Dashboard HTTPS") |
| 6 | Medium | Pillow 11.3.0 had 73 published vulnerabilities and decodes pictures fetched over HTTP | Fixed (PR #46): Pillow 13.0.0, JPEG-only decoding |
| 7 | Medium-Low | NVR ISAPI is plain HTTP (Digest) with an admin login: someone on the camera network can read or fake NVR replies | Probe available: `probe_nvr_features --ask-login` (restricted login), `--https` (certificate pin); open: HTTPS scans per NVR |
| 8 | Low-Medium | Containers run as root; every `env_file` service gets the whole `.env` | Fixed: `no-new-privileges` (PR #46), UID 10001, no capabilities. Open: split secrets |
| 9 | Low | `DASHBOARD_BIND_IP=0.0.0.0` exposes the dashboard on every interface; Docker-published ports bypass UFW | **Server setting**: the server's LAN IP |
| 10 | Low | Deploy runs whatever is on GitHub `main` as root | Open: GitHub 2FA, branch protection on `main`, pin the base image digest |
| 11 | Low | Deploy backups (with `.env`) were created world-readable before `chmod 600`, and are never rotated | Fixed: created 600 (PR #46); one backup kept, replaced by each deploy |
| 12 | Info | Real NVR IP, site and camera names in docs and diagnostics | Fixed (restructure): diagnostics ask for `--ip`; docs use example addresses |

Checked without findings: no `.env`, inventory, token or real password in any
commit; POST requests need a custom header and a JSON body (CSRF);
strict CSP and `textContent` rendering; snapshot, PDF and saved-report
downloads accept validated IDs only and refuse symlinks; CSV formula escaping;
PDF, e-mail and Telegram HTML escaping; SMTP with verified TLS; the bot token
never reaches a log; the scanner re-validates queued scan targets; no
`shell=True`.

## Check the two server settings

Prints only the mode and the bind address, no secret:

```bash
cd /opt/NVR_Track && sudo grep -E '^(TELEGRAM_ACCESS|DASHBOARD_BIND_IP)=' .env
```

To change them: edit `.env` (keep `chmod 600`), then
`docker compose up -d --force-recreate nvr-dashboard nvr-telegram`.

## In place

- NVRs are read-only: GET requests plus ISAPI recording search and
  `dailyDistribution` queries; Digest authentication.
- Dashboard: Basic auth (password ≥ 16 characters), per-address login lockout,
  30 s connection timeout, CSP `default-src 'none'` with scripts and styles
  from the server only, `X-Frame-Options: DENY`, `nosniff`, `no-store`,
  whitelisted JSON fields (no raw report content, no credentials).
- Separation: the dashboard and the bot have no NVR or SMTP credentials; they
  queue scans through a file that the scanner re-validates.
- Telegram: allowlist / group / open access modes, admins only for scans,
  notes (default), pictures and group alert switches; per-user rate limit;
  token never logged.
- Containers: UID/GID 10001, no Linux capabilities, `no-new-privileges`;
  secrets only from `.env` (chmod 600, root, git-ignored, not in the build
  context); inventory and HTTPS key 640 root:10001.
- Deploy: backup before every change, tests before every rebuild, refuses to
  pull over server edits.
- CI: tests, Docker test image and a dependency audit on every pull request.

## Next steps (not done)

1. **Dashboard TLS** — turn on `DASHBOARD_TLS` (docs/operations.md "Dashboard HTTPS").
2. **NVR account** — a dedicated NVR user with only the rights the checks need
   (no configuration rights), instead of an admin account: try it with
   `--ask-login` on one NVR (docs/operations.md), then `NVR_<NN>_USERNAME/PASSWORD`.
3. **Split secrets** — give `nvr-events` and `nvr-live` only the NVR
   credentials instead of the whole `.env`.
4. **NVR HTTPS** — `--https` probe on one NVR (docs/operations.md), then
   per-NVR opt-in scans pinned to the probed certificate.
5. **Supply chain** — 2FA on GitHub, branch protection on `main` (pull request
   required), pin `python:3.12-slim` by digest, `pip install --require-hashes`.
