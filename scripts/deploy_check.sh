#!/usr/bin/env bash
# Deploy the latest main branch and verify the whole NVR_Track installation.
#
# Run on the Ubuntu server:  cd /opt/NVR_Track && sudo bash scripts/deploy_check.sh
#
# Read-only towards the NVRs. Never prints .env values or credentials.
# Sends no email (runs one report per NVR instead of the fleet runner).
# Touches only the nvr-dashboard container; no other service is restarted.
set -u

APP_DIR=${APP_DIR:-/opt/NVR_Track}
STAMP=$(date +%Y%m%d-%H%M%S)
PASS=0; WARN=0; FAIL=0

cd "$APP_DIR" || { echo "Cannot open $APP_DIR"; exit 1; }
if [ -n "${NVR_DEPLOY_CHECK_LOG:-}" ]; then
    LOG=$NVR_DEPLOY_CHECK_LOG  # Restarted after updating itself; output is already logged.
else
    LOG=/root/nvr-track-check-$STAMP.txt
    : > "$LOG" && chmod 600 "$LOG"
    exec > >(tee -a "$LOG") 2>&1
fi

pass() { PASS=$((PASS + 1)); echo "  [PASS] $*"; }
warn() { WARN=$((WARN + 1)); echo "  [WARN] $*"; }
fail() { FAIL=$((FAIL + 1)); echo "  [FAIL] $*"; }
section() { echo; echo "== $* =="; }

section "1. Backup"
BACKUP=/root/nvr-track-backup-$STAMP.tgz
if tar --exclude=./reports -czf "$BACKUP" -C "$APP_DIR" . 2>/dev/null; then
    chmod 600 "$BACKUP"; pass "Backup saved to $BACKUP (permissions 600)"
else
    fail "Backup failed; stopping before any change"; exit 1
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
if docker build -q --target test -t nvr-track:test . >/dev/null; then
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

section "4. Build and restart dashboard"
if docker compose build -q nvr-report nvr-report-all nvr-dashboard; then
    pass "Runtime images built"
else
    fail "Runtime image build failed"; exit 1
fi
if docker compose up -d --force-recreate nvr-dashboard >/dev/null 2>&1; then
    pass "Dashboard container recreated"
else
    fail "Dashboard container did not start"
fi
sleep 5
STATE=$(docker inspect -f '{{.State.Status}}' "$(docker compose ps -q nvr-dashboard)" 2>/dev/null)
[ "$STATE" = "running" ] && pass "Dashboard is running" || fail "Dashboard state: ${STATE:-missing}"
ADDR=$(docker compose port nvr-dashboard 8080 2>/dev/null | head -1)
if [ -n "$ADDR" ]; then
    CODE=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://$ADDR/")
    [ "$CODE" = "401" ] && pass "Dashboard at http://$ADDR asks for a password (HTTP 401)" \
        || fail "Dashboard at http://$ADDR returned HTTP $CODE without a password"
else
    fail "Dashboard port 8080 is not published"
fi

section "5. Fresh report for every NVR (read-only, no email)"
IPS=$(docker compose run --rm -T --entrypoint python nvr-report </dev/null -c \
    "from app.config import load_inventory
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
done

section "6. Dashboard data (what the web page shows)"
DASH=$(docker compose exec -T nvr-dashboard python - 2>&1 <<'PY'
from collections import Counter
from app.dashboard import read_dashboard_data

data = read_dashboard_data()
s = data["summary"]
for key in ("total_nvrs", "latest_run_reporting", "healthy", "warning", "critical",
            "cameras_online", "cameras_offline", "cameras_unknown",
            "recording_evidence", "recording_no_evidence", "recording_unverified",
            "recording_errors", "recording_not_checked", "cameras_without_result",
            "offline_with_evidence", "storage_warnings", "storage_critical", "stale_reports"):
    print(f"  {key:24} {s.get(key)}")
print()
print(f"  {'NVR':7} {'OVERALL':9} {'STORAGE':9} {'ROWS':>7}  RECORDING RESULTS")
problems = 0
for nvr in data["nvrs"]:
    rows = len(nvr["channels"])
    expected = nvr["expected_channels"]
    counts = Counter(ch["recording_result"]["status"] for ch in nvr["channels"])
    text = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no camera rows"
    print(f"  {nvr['id']:7} {nvr['overall_status']:9} {nvr['storage_status']:9} {rows:>3}/{expected!s:<3}  {text}")
    if not isinstance(expected, int) or rows < expected:
        problems += 1
print()
print(f"CHECK total_nvrs={s.get('total_nvrs')}")
print(f"CHECK incomplete_nvrs={problems}")
print(f"CHECK unverified={s.get('recording_unverified', 0)}")
print(f"CHECK no_result={s.get('cameras_without_result', 0)}")
PY
)
echo "$DASH" | grep -v '^CHECK '
TOTAL=$(echo "$DASH" | sed -n 's/^CHECK total_nvrs=//p')
INCOMPLETE=$(echo "$DASH" | sed -n 's/^CHECK incomplete_nvrs=//p')
UNVERIFIED=$(echo "$DASH" | sed -n 's/^CHECK unverified=//p')
NORESULT=$(echo "$DASH" | sed -n 's/^CHECK no_result=//p')
[ "${TOTAL:-0}" = "12" ] && pass "All 12 NVRs are on the dashboard" || fail "Dashboard shows ${TOTAL:-0} NVRs, expected 12"
if [ "${INCOMPLETE:-1}" = "0" ]; then
    pass "Every NVR has a row for every expected camera"
else
    warn "$INCOMPLETE NVR(s) missing camera rows ($NORESULT cameras with no result) - NVR unreachable; see section 9"
fi
[ "${UNVERIFIED:-1}" = "0" ] && pass "No unverified or failed recording checks" \
    || warn "$UNVERIFIED camera recording checks unverified/failed (usually network loss)"

section "7. Daily schedule"
CRON=$( { crontab -l 2>/dev/null; cat /etc/cron.d/* 2>/dev/null; } | grep -v '^#' | grep 'nvr-report-all' | head -1)
if echo "$CRON" | grep -qE '^0 8 \* \* \*'; then
    pass "Daily report at 08:00: $CRON"
else
    fail "No 08:00 cron line for nvr-report-all"
fi
TZNAME=$(timedatectl show -p Timezone --value 2>/dev/null)
[ "$TZNAME" = "Asia/Phnom_Penh" ] && pass "Server time zone is Asia/Phnom_Penh" || warn "Server time zone is ${TZNAME:-unknown}"
LAST=$(grep -h "Email report:" reports/scheduler.log 2>/dev/null | tail -1)
case "$LAST" in
    *SENT*) pass "Last scheduled email: $LAST" ;;
    "") warn "No email result in reports/scheduler.log yet (first 08:00 run will write it)" ;;
    *) fail "Last scheduled email: $LAST" ;;
esac

section "8. Secrets and private files"
PERM=$(stat -c %a .env 2>/dev/null)
[ "$PERM" = "600" ] && pass ".env permissions are 600" || fail ".env permissions are ${PERM:-missing}"
for f in .env config/nvrs.local.json reports/x.json; do
    git check-ignore -q "$f" && pass "$f is ignored by Git" || fail "$f is NOT ignored by Git"
done
TRACKED=$(git ls-files | grep -E '(^|/)\.env$|nvrs\.local\.json|^reports/')
[ -z "$TRACKED" ] && pass "No secrets, inventory or reports are committed" || fail "Committed private files: $TRACKED"

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
