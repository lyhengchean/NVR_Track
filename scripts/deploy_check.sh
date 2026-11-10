#!/usr/bin/env bash
# Deploy the latest main branch and verify the whole NVR_Track installation.
#
# Run on the Ubuntu server:  cd /opt/NVR_Track && sudo bash scripts/deploy_check.sh
#
# Read-only towards the NVRs. Never prints .env values or credentials.
# Sends no email (runs one report per NVR instead of the fleet runner).
# Recreates only nvr-dashboard, nvr-scanner and (if configured) nvr-telegram / nvr-events / nvr-live.
set -u

APP_DIR=${APP_DIR:-/opt/NVR_Track}
STAMP=$(date +%Y%m%d-%H%M%S)
PASS=0; WARN=0; FAIL=0

cd "$APP_DIR" || { echo "Cannot open $APP_DIR"; exit 1; }
if [ -n "${NVR_DEPLOY_CHECK_LOG:-}" ]; then
    LOG=$NVR_DEPLOY_CHECK_LOG  # Restarted after updating itself; output is already logged.
else
    LOG=/root/nvr-track-check-$STAMP.txt
    (umask 077 && : > "$LOG") && chmod 600 "$LOG"
    exec > >(tee -a "$LOG") 2>&1
fi

pass() { PASS=$((PASS + 1)); echo "  [PASS] $*"; }
warn() { WARN=$((WARN + 1)); echo "  [WARN] $*"; }
fail() { FAIL=$((FAIL + 1)); echo "  [FAIL] $*"; }
section() { echo; echo "== $* =="; }

section "1. Backup"
# One backup, replaced by every deploy. It holds .env: private (600) from its
# first byte. The new one is written beside the old one, checked, and only then
# moved over it; older backups are removed once the new one is safe.
BACKUP_DIR=${NVR_BACKUP_DIR:-/root}
BACKUP=$BACKUP_DIR/nvr-track-backup.tgz
prune_backups() {  # Remove every other NVR_Track backup; $1 is the one to keep.
    find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'nvr-track-backup.tgz' -o -name 'nvr-track-backup-*.tgz' \) \
        ! -path "$1" -delete
}
PREVIOUS=$(ls -t "$BACKUP_DIR"/nvr-track-backup.tgz "$BACKUP_DIR"/nvr-track-backup-*.tgz 2>/dev/null | head -1)
if [ -n "${NVR_DEPLOY_CHECK_LOG:-}" ] && [ -n "$PREVIOUS" ] && gzip -t "$PREVIOUS" 2>/dev/null; then
    # Restarted after the pull: keep the backup taken just before the pull.
    [ "$PREVIOUS" = "$BACKUP" ] || mv -f "$PREVIOUS" "$BACKUP" 2>/dev/null || BACKUP=$PREVIOUS
    prune_backups "$BACKUP"
    pass "Backup from before the pull kept: $BACKUP"
else
    NEW=$(mktemp "$BACKUP_DIR/.nvr-track-backup.XXXXXX") || { fail "Cannot write a backup in $BACKUP_DIR; stopping before any change"; exit 1; }
    if (umask 077 && tar --exclude=./reports -czf "$NEW" -C "$APP_DIR" .) 2>/dev/null && gzip -t "$NEW" 2>/dev/null; then
        chmod 600 "$NEW"
        if mv -f "$NEW" "$BACKUP" 2>/dev/null; then
            prune_backups "$BACKUP"
            pass "Backup saved to $BACKUP ($(du -h "$BACKUP" | cut -f1), permissions 600); older backups removed"
        elif mv -f "$NEW" "$BACKUP_DIR/nvr-track-backup-$STAMP.tgz" 2>/dev/null; then
            # Could not replace the old file: keep the new one under its own name instead.
            BACKUP=$BACKUP_DIR/nvr-track-backup-$STAMP.tgz
            prune_backups "$BACKUP"
            pass "Backup saved to $BACKUP (permissions 600); older backups removed"
        else
            BACKUP=$NEW
            warn "Backup saved as $NEW but could not be renamed; older backups kept"
        fi
    else
        rm -f "$NEW"
        fail "Backup failed (previous backup kept); stopping before any change"; exit 1
    fi
fi

section "2. Source code"
if ! git diff --quiet || ! git diff --cached --quiet; then
    fail "Tracked files were edited on the server; not pulling. Review: git status"
    exit 1
fi
BEFORE=$(git rev-parse HEAD)
if git pull --ff-only -q; then
    pass "Pulled main: $(git log --oneline -1)"
else
    fail "git pull --ff-only failed"; exit 1
fi
# Bash keeps running the copy of this script it started with. If the pull
# changed this file, restart once so every later section is the new version.
if [ -z "${NVR_DEPLOY_CHECK_LOG:-}" ] && ! git diff --quiet "$BEFORE" HEAD -- scripts/deploy_check.sh; then
    echo "  deploy_check.sh was updated by this pull; restarting it"
    NVR_DEPLOY_CHECK_LOG=$LOG exec bash "$APP_DIR/scripts/deploy_check.sh"
fi
if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
    pass "Server matches GitHub main"
else
    fail "Server HEAD differs from origin/main"
fi

section "3. Tests (isolated test image, no NVR contact)"
if docker build -q -f deploy/Dockerfile --target test -t nvr-track:test . >/dev/null; then
    RESULT=$(docker run --rm nvr-track:test 2>&1 | tail -1)
    echo "  $RESULT"
    if echo "$RESULT" | grep -q "passed" && ! echo "$RESULT" | grep -qE "failed|error"; then
        pass "Test suite passed"
    else
        fail "Test suite did not pass"
    fi
else
    fail "Test image build failed"
fi

section "4. Build and restart dashboard and scanner"
if docker compose build -q nvr-report nvr-report-all nvr-dashboard nvr-scanner nvr-telegram nvr-events nvr-live; then
    pass "Runtime images built"
else
    fail "Runtime image build failed"; exit 1
fi
# The containers run as UID/GID 10001: give them reports/, state/ and read access
# to the inventory and HTTPS key before they start (only what differs).
if CHANGED=$(bash scripts/container_files.sh); then
    pass "Container user 10001 owns reports/ and state/ ($CHANGED changed) and can read the inventory and HTTPS key"
else
    fail "Could not give the container user its files; containers may fail to write (sudo bash scripts/container_files.sh)"
fi
if docker compose up -d --force-recreate nvr-dashboard nvr-scanner >/dev/null 2>&1; then
    pass "Dashboard and scanner containers recreated"
else
    fail "Dashboard or scanner container did not start"
fi
SERVICES="nvr-dashboard nvr-scanner"
# The Telegram bot runs only once a token is set (the value is never printed).
if grep -qE '^TELEGRAM_BOT_TOKEN=.+' .env 2>/dev/null; then
    if docker compose up -d --force-recreate nvr-telegram >/dev/null 2>&1; then
        pass "Telegram bot container recreated"
    else
        fail "Telegram bot container did not start"
    fi
    SERVICES="$SERVICES nvr-telegram"
else
    echo "  Telegram bot not configured (no TELEGRAM_BOT_TOKEN in .env); skipped"
fi
# Real-time events run only for NVRs listed in NVR_EVENT_NVR_IDS.
if grep -qE '^NVR_EVENT_NVR_IDS=.+' .env 2>/dev/null; then
    if docker compose up -d --force-recreate nvr-events >/dev/null 2>&1; then
        pass "Real-time event listener recreated"
    else
        fail "Real-time event listener did not start"
    fi
    SERVICES="$SERVICES nvr-events"
else
    echo "  Real-time events not enabled (NVR_EVENT_NVR_IDS empty); skipped"
fi
# Live status between scans runs only for NVRs listed in NVR_LIVE_NVR_IDS.
if grep -qE '^NVR_LIVE_NVR_IDS=.+' .env 2>/dev/null; then
    if docker compose up -d --force-recreate nvr-live >/dev/null 2>&1; then
        pass "Live status checker recreated"
    else
        fail "Live status checker did not start"
    fi
    SERVICES="$SERVICES nvr-live"
else
    echo "  Live status not enabled (NVR_LIVE_NVR_IDS empty); skipped"
    docker compose stop nvr-live >/dev/null 2>&1 || true
    rm -f state/live/service.json  # Switched off: no "live checks stopped" alert.
fi
sleep 5
for svc in $SERVICES; do
    CID=$(docker compose ps -q $svc)
    STATE=$(docker inspect -f '{{.State.Status}}' "$CID" 2>/dev/null)
    RUNAS=$(docker inspect -f '{{.Config.User}}' "$CID" 2>/dev/null)
    [ "$STATE" = "running" ] && pass "$svc is running as user ${RUNAS:-root}" || fail "$svc state: ${STATE:-missing}"
    case "$RUNAS" in
        ""|0|0:*|root|root:*) warn "$svc runs as root; rebuild: docker compose build $svc" ;;
    esac
done
if [ "${SERVICES#*nvr-telegram}" != "$SERVICES" ]; then
    sleep 10
    if docker compose logs --tail 20 nvr-telegram 2>/dev/null | grep -q "telegram: running as"; then
        pass "Telegram bot connected: $(docker compose logs --tail 20 nvr-telegram 2>/dev/null | sed -n 's/.*telegram: running as \(@[^;]*\).*/\1/p' | tail -1)"
    else
        warn "Telegram bot not connected yet; check: docker compose logs --tail 20 nvr-telegram"
    fi
fi
ADDR=$(docker compose port nvr-dashboard 8080 2>/dev/null | head -1)
TLS=$(sed -n 's/^DASHBOARD_TLS=//p' .env 2>/dev/null | tail -1 | tr -d "\"' \r" | tr 'A-Z' 'a-z')
if [ -z "$ADDR" ]; then
    fail "Dashboard port 8080 is not published"
elif [ "$TLS" = "true" ] || [ "$TLS" = "1" ] || [ "$TLS" = "yes" ] || [ "$TLS" = "on" ]; then
    # Self-signed certificate: -k here; each browser shows a warning once.
    CODE=$(curl -sk -o /dev/null -m 10 -w '%{http_code}' "https://$ADDR/")
    [ "$CODE" = "401" ] && pass "Dashboard at https://$ADDR asks for a password (HTTP 401)" \
        || fail "Dashboard at https://$ADDR returned HTTP $CODE without a password"
    REDIRECT=$(curl -s -o /dev/null -m 10 -w '%{http_code} %{redirect_url}' "http://$ADDR/")
    [ "${REDIRECT%% *}" = "301" ] && pass "http://$ADDR redirects to ${REDIRECT#* }" \
        || warn "http://$ADDR did not redirect to https:// (HTTP ${REDIRECT%% *})"
    if openssl x509 -checkend $((30 * 86400)) -noout -in config/tls/dashboard.crt >/dev/null 2>&1; then
        pass "Dashboard certificate valid until $(openssl x509 -enddate -noout -in config/tls/dashboard.crt | cut -d= -f2)"
    else
        warn "Dashboard certificate expires within 30 days or cannot be read; renew: sudo bash scripts/dashboard_cert.sh --renew"
    fi
else
    CODE=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://$ADDR/")
    [ "$CODE" = "401" ] && pass "Dashboard at http://$ADDR asks for a password (HTTP 401)" \
        || fail "Dashboard at http://$ADDR returned HTTP $CODE without a password"
    echo "  Dashboard uses plain HTTP (DASHBOARD_TLS is off; see docs/security.md)"
fi

section "5. Fresh report for every NVR (read-only, no email)"
IPS=$(docker compose run --rm -T --entrypoint python nvr-report </dev/null -c \
    "from nvr_track.core.config import load_inventory
for n in load_inventory('config/nvrs.local.json'): print(n.ip)" 2>/dev/null)
COUNT=$(echo "$IPS" | grep -c .)
echo "  NVRs in inventory: $COUNT. Checked one at a time; each can take up to"
echo "  NVR_RECORDING_MAX_NVR_SECONDS (default 300 s) plus about a minute."
for ip in $IPS; do
    START=$(date +%s)
    echo "  ... $ip started $(date +%H:%M:%S)"
    # The nvr-report service keeps stdin open. Without --foreground, timeout
    # moves compose out of the terminal's foreground group, and touching the
    # terminal there stops the whole group (state T) so even the timeout never
    # fires. --foreground and </dev/null each prevent that on their own.
    OUT=$(timeout --foreground -k 30 900 docker compose run --rm -T nvr-report --ip "$ip" </dev/null 2>&1)
    ID=$(echo "$OUT" | sed -n 's/^Checking NVR \([^ ]*\) at.*/\1/p')
    OVERALL=$(echo "$OUT" | sed -n 's/^Overall status: //p')
    PERCAM=$(echo "$OUT" | sed -n 's/^Per-camera recording: //p')
    DEVICE=$(echo "$OUT" | sed -n 's/^Device information: //p')
    printf '  %-7s %-15s %-9s device=%-22s %s (%ss)\n' "${ID:-?}" "$ip" "${OVERALL:-NO_REPORT}" "${DEVICE:-?}" "$PERCAM" "$(( $(date +%s) - START ))"
    echo "$OUT" | sed -n 's/^Critical: /            critical: /p' | head -10
    echo "$OUT" | sed -n 's/^Warning: /            warning: /p' | head -10
    echo "$OUT" | sed -n 's/^Note: /            note: /p' | head -5
done

section "6. Dashboard data (what the web page shows)"
DASH=$(docker compose exec -T nvr-dashboard python - 2>&1 <<'PY'
from collections import Counter
from nvr_track.web.dashboard import read_dashboard_data

data = read_dashboard_data()
s = data["summary"]
for key in ("total_nvrs", "latest_run_reporting", "healthy", "warning", "critical",
            "cameras_online", "cameras_offline", "cameras_unknown",
            "recording_evidence", "recording_no_evidence", "recording_unverified",
            "recording_errors", "recording_not_checked", "cameras_not_listed", "cameras_not_configured",
            "offline_with_evidence", "storage_warnings", "storage_critical", "smart_problems",
            "camera_changes", "stale_reports"):
    print(f"  {key:24} {s.get(key)}")
print()
print(f"  {'NVR':7} {'OVERALL':9} {'STORAGE':9} {'ROWS':>7}  {'KEPT':7} RECORDING RESULTS")
problems = 0
for nvr in data["nvrs"]:
    rows = len(nvr["channels"])
    # KEPT: days of footage, typical/fewest camera recording now ("30+" = whole calendar).
    expected = nvr["expected_channels"]
    counts = Counter(ch["recording_result"]["status"] for ch in nvr["channels"])
    text = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no camera rows"
    k = nvr.get("kept_days") or {}
    kept = "?" if not k.get("cameras") else (
        (f"{k['typical']}+" if k["typical"] >= k["window"] else str(k["typical"]))
        + (f"/{k['fewest']}" if k["fewest"] < k["typical"] else ""))
    print(f"  {nvr['id']:7} {nvr['overall_status']:9} {nvr['storage_status']:9} {rows:>3}/{expected!s:<3}  {kept:7} {text}")
    # Missing camera rows, with the real reason (nvr_track.web.dashboard.missing_cameras).
    gone = nvr.get("missing_cameras") or {}
    if not isinstance(expected, int):
        problems += 1
        reason = "failed" if nvr.get("last_run_failed") else "noexpected"
        print(f"CHECK missing={nvr['id']} {reason} 0 {nvr.get('last_run_failure_code') or '-'} -")
    elif rows < expected:
        problems += 1
        if gone.get("not_configured"):
            print(f"CHECK missing={nvr['id']} config {gone['not_configured']} {nvr.get('configured_channels')} {expected}")
        if gone.get("not_listed"):
            print(f"CHECK missing={nvr['id']} unlisted {gone['not_listed']} - {expected}")
fleet = data.get("fleet_duplicates") or []
print()
print(f"  Cameras recorded by more than one NVR: {len(fleet)}")
for group in fleet[:10]:
    print("    " + " = ".join(f"{c['nvr']} ch{c['channel_id']} {c['name']}".strip() for c in group["cameras"]))
print()
print(f"CHECK total_nvrs={s.get('total_nvrs')}")
print(f"CHECK incomplete_nvrs={problems}")
print(f"CHECK unverified={s.get('recording_unverified', 0)}")
PY
)
echo "$DASH" | grep -v '^CHECK '
TOTAL=$(echo "$DASH" | sed -n 's/^CHECK total_nvrs=//p')
INCOMPLETE=$(echo "$DASH" | sed -n 's/^CHECK incomplete_nvrs=//p')
UNVERIFIED=$(echo "$DASH" | sed -n 's/^CHECK unverified=//p')
[ "${TOTAL:-0}" = "12" ] && pass "All 12 NVRs are on the dashboard" || fail "Dashboard shows ${TOTAL:-0} NVRs, expected 12"
if [ "${INCOMPLETE:-1}" = "0" ]; then
    pass "Every NVR has a row for every expected camera"
else
    while read -r ID KIND N A B; do
        case "$KIND" in
            config) warn "$ID has $A channels configured, the inventory expects $B: $N expected camera(s) not on the NVR. Removed on purpose? Then set expected_channels to $A for $ID in config/nvrs.local.json" ;;
            unlisted) warn "$ID: $N of $B expected cameras not in the latest report (NVR unreachable or channel list incomplete); see section 5" ;;
            failed) warn "$ID: latest scan failed ($A) and there is no saved report" ;;
            noexpected) warn "$ID has no expected_channels in config/nvrs.local.json; missing cameras cannot be detected" ;;
        esac
    done <<< "$(echo "$DASH" | sed -n 's/^CHECK missing=//p')"
fi
[ "${UNVERIFIED:-1}" = "0" ] && pass "No unverified or failed recording checks" \
    || warn "$UNVERIFIED camera recording checks unverified/failed (usually network loss)"

section "7. Container health, automatic scans and email"
# Docker healthchecks (backend/nvr_track/services/healthcheck.py; local only). A new container
# is "starting" until its first check, about a minute after it started.
for svc in $SERVICES; do
    CID=$(docker compose ps -q "$svc" 2>/dev/null)
    HEALTH=""
    for _ in $(seq 1 18); do
        HEALTH=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CID" 2>/dev/null)
        [ "$HEALTH" = "starting" ] || break
        sleep 5
    done
    case "$HEALTH" in
        healthy) pass "$svc healthcheck: healthy" ;;
        starting) warn "$svc healthcheck still starting; check later: docker compose ps" ;;
        none) warn "$svc has no healthcheck; recreate it: docker compose up -d --force-recreate $svc" ;;
        *) fail "$svc healthcheck: ${HEALTH:-unknown} ($(docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' "$CID" 2>/dev/null | grep . | tail -1))" ;;
    esac
done
# Real-time event streams (nvr-events): one per NVR in NVR_EVENT_NVR_IDS.
if echo " $SERVICES " | grep -q " nvr-events "; then
    STREAMS=$(docker compose exec -T nvr-events python - 2>/dev/null <<'PY'
from nvr_track.nvr import events
from nvr_track.core.config import load_inventory
streams = events.read_status().get("nvrs") or {}
for nvr, state in sorted(streams.items()):
    if state.get("connected"):
        print(f"UP {nvr}")
    else:
        print(f"DOWN {nvr} {events.reason_text(state.get('last_error'))}")
for nvr in load_inventory("config/nvrs.local.json"):
    if nvr.id not in streams:
        print(f"OFF {nvr.id}")
PY
)
    UP=$(echo "$STREAMS" | grep -c '^UP ')
    TOTAL=$(echo "$STREAMS" | grep -cE '^(UP|DOWN) ')
    if [ "$TOTAL" -eq 0 ]; then
        warn "Event listener reports no streams yet; check: docker compose logs --tail 20 nvr-events"
    else
        [ "$UP" -gt 0 ] && pass "Real-time event streams connected: $UP of $TOTAL"
        while read -r ID WHY; do
            [ -n "$ID" ] && warn "$ID real-time event stream not connected: $WHY"
        done <<< "$(echo "$STREAMS" | sed -n 's/^DOWN //p')"
    fi
    OFF=$(echo "$STREAMS" | sed -n 's/^OFF //p' | tr '\n' ' ')
    [ -n "${OFF// /}" ] && echo "  No real-time events (not in NVR_EVENT_NVR_IDS): $OFF"
fi
# Live status (nvr-live): first check runs right after start.
if echo " $SERVICES " | grep -q " nvr-live "; then
    sleep 20
    LIVE=$(docker compose exec -T nvr-dashboard python - 2>/dev/null <<'PY'
from nvr_track.monitoring import live
service, entries = live.read_all()
print("ALIVE=" + ("yes" if live.service_alive(service) else "no"))
for nvr in service.get("nvrs") or []:
    e = entries.get(nvr)
    if not e:
        print(f"WAIT {nvr}")
    elif e.get("reachable") is True:
        ch = e.get("channels") or {}
        disks = [d for d in (e.get("storage") or {}).get("disks") or []
                 if str(d.get("reported_status")).lower() not in {"ok", "normal", "idle", "notexist"}]
        print(f"OK {nvr} {sum(v == 'ONLINE' for v in ch.values())} online, "
              f"{sum(v == 'OFFLINE' for v in ch.values())} offline; disks not OK: "
              + (", ".join(f"HDD {d.get('id')} {d.get('reported_status')}" for d in disks) or "none"))
    else:
        print(f"BAD {nvr} {e.get('error') or 'no answer'}")
PY
)
    if echo "$LIVE" | grep -q '^ALIVE=yes'; then
        pass "Live status checker running ($(echo "$LIVE" | grep -cE '^(OK|BAD|WAIT) ') NVRs)"
    else
        warn "Live status checker has no heartbeat yet; check: docker compose logs --tail 20 nvr-live"
    fi
    while read -r KIND ID REST; do
        case "$KIND" in
            OK) pass "Live $ID: $REST" ;;
            BAD) warn "Live $ID not readable: $REST" ;;
            WAIT) warn "Live $ID: no check yet; check: docker compose logs --tail 20 nvr-live" ;;
        esac
    done <<< "$(echo "$LIVE" | grep -E '^(OK|BAD|WAIT) ')"
fi
SCAN=$(docker compose exec -T nvr-dashboard python - 2>/dev/null <<'PY'
from nvr_track.web.dashboard import read_scan_status
s = read_scan_status()
print("AVAILABLE=" + ("yes" if s["available"] else "no"))
print("TIMES=" + ",".join(s["scan_times"]))
print("EMAIL=" + ",".join(s["email_times"]))
print("NEXT=" + (s["next_scheduled_at"] or ""))
print("LASTEMAIL=" + ((s["last_email"] or {}).get("result") or ""))
PY
)
value() { echo "$SCAN" | sed -n "s/^$1=//p"; }
if [ "$(value AVAILABLE)" = "yes" ]; then
    pass "Scanner is running; automatic scans at $(value TIMES) (email at $(value EMAIL))"
else
    fail "Scanner is not reporting; check: docker compose logs --tail 50 nvr-scanner"
fi
[ -n "$(value TIMES)" ] || warn "No automatic scan times; set NVR_SCAN_TIMES in .env"
CRON=$( { crontab -l 2>/dev/null; cat /etc/cron.d/* 2>/dev/null; } | grep -v '^#' | grep 'nvr-report-all' | head -1)
if [ -n "$CRON" ]; then
    warn "Old cron line still runs nvr-report-all (a second scan and email at the same time). Remove it; see README 'Scan now and automatic scans'"
else
    pass "No old cron line; the scanner is the only schedule"
fi
TZNAME=$(timedatectl show -p Timezone --value 2>/dev/null)
[ "$TZNAME" = "Asia/Phnom_Penh" ] && pass "Server time zone is Asia/Phnom_Penh" || warn "Server time zone is ${TZNAME:-unknown}"
case "$(value LASTEMAIL)" in
    SENT) pass "Last scheduled email: SENT" ;;
    "") warn "No email from the scanner yet; the next email run writes it" ;;
    *) fail "Last scheduled email: $(value LASTEMAIL); check SMTP settings and: docker compose logs nvr-scanner" ;;
esac

section "8. Secrets and private files"
PERM=$(stat -c %a .env 2>/dev/null)
[ "$PERM" = "600" ] && pass ".env permissions are 600" || fail ".env permissions are ${PERM:-missing}"
for f in .env config/nvrs.local.json config/tls/dashboard.key reports/x.json; do
    git check-ignore -q "$f" && pass "$f is ignored by Git" || fail "$f is NOT ignored by Git"
done
TRACKED=$(git ls-files | grep -E '(^|/)\.env$|nvrs\.local\.json|^reports/|^config/tls/')
[ -z "$TRACKED" ] && pass "No secrets, inventory or reports are committed" || fail "Committed private files: $TRACKED"
# Readable by root and the containers' group 10001 only.
for f in config/nvrs.local.json config/tls/dashboard.key; do
    [ -e "$f" ] || continue
    FPERM=$(stat -c '%a %U:%g' "$f")
    [ "$FPERM" = "640 root:10001" ] && pass "$f: 640, root, group 10001" \
        || fail "$f is $FPERM (needs 640 root:10001); fix: sudo bash scripts/container_files.sh"
done

section "9. Server network (read-only)"
IFACE=$(ip route show default | awk '{print $5; exit}')
GW=$(ip route show default | awk '{print $3; exit}')
LOSS=$(ping -c 50 -i 0.2 -q "$GW" 2>/dev/null | sed -n 's/.* \([0-9.]*\)% packet loss.*/\1/p')
DROPS=$(ip -s link show "$IFACE" | awk '/RX:/{getline; print $4; exit}')
echo "  Interface $IFACE, gateway $GW, packet loss ${LOSS:-?}%, RX dropped total ${DROPS:-?}"
if [ "${LOSS%.*}" = "0" ]; then
    pass "No packet loss to the gateway"
else
    warn "Packet loss ${LOSS:-?}% to the gateway: server/VMware network fault (not NVR_Track)"
fi

section "10. Other services (report only, not touched)"
docker ps -a --format '{{.Names}}  {{.Status}}' | grep -iv 'nvr' | sed 's/^/  /' || true
systemctl is-active traccar >/dev/null 2>&1 && echo "  traccar (systemd): active" || true

echo
echo "======================================================"
echo " RESULT: $PASS PASS, $WARN WARN, $FAIL FAIL"
echo " Full log: $LOG"
echo "======================================================"
sleep 1
[ "$FAIL" = "0" ]
