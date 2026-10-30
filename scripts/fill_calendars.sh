#!/usr/bin/env bash
# Fill every camera's recording calendar and history columns, then keep them filled.
#
# Run on the Ubuntu server:  cd /opt/NVR_Track && sudo bash scripts/fill_calendars.sh
#
# 1. Sets three NVR_Track settings in .env (backup first; values never printed):
#      NVR_RETENTION_MAX_CAMERAS_PER_RUN=32   calendar checks per NVR per run (was 2)
#      NVR_HISTORICAL_MAX_CAMERAS_PER_RUN=8   history searches per NVR per run (was 2)
#      NVR_DEFAULT_RECORDING_EXPECTATION=continuous
# 2. Rebuilds the images and recreates only nvr-dashboard.
# 3. Runs read-only reports NVR by NVR (no email) until no calendar is pending
#    (at most 4 passes).
set -u

APP_DIR=${APP_DIR:-/opt/NVR_Track}
STAMP=$(date +%Y%m%d-%H%M%S)
LOG=/root/nvr-track-fill-$STAMP.txt
cd "$APP_DIR" || { echo "Cannot open $APP_DIR"; exit 1; }
: > "$LOG" && chmod 600 "$LOG"
exec > >(tee -a "$LOG") 2>&1

set_env() {  # set KEY=VALUE in .env without printing anything else from the file
    if grep -q "^$1=" .env; then
        sed -i "s|^$1=.*|$1=$2|" .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

echo "== 1. Settings =="
cp -p .env "/root/env-backup-$STAMP" && chmod 600 "/root/env-backup-$STAMP"
echo "  .env backed up to /root/env-backup-$STAMP"
set_env NVR_RETENTION_MAX_CAMERAS_PER_RUN 32
set_env NVR_HISTORICAL_MAX_CAMERAS_PER_RUN 8
set_env NVR_DEFAULT_RECORDING_EXPECTATION continuous
chmod 600 .env
echo "  Set: calendar checks 32/run, history searches 8/run, default expectation continuous"
if grep -q '^NVR_RETENTION_ENABLED=true' .env; then
    echo "  Calendar checks are enabled"
else
    echo "  NOTE: NVR_RETENTION_ENABLED is not true; calendars stay 'Not configured'"
fi

echo
echo "== 2. Build and restart dashboard =="
docker compose build -q nvr-report nvr-report-all nvr-dashboard || { echo "  Build failed"; exit 1; }
docker compose up -d --force-recreate nvr-dashboard >/dev/null 2>&1 && echo "  Dashboard recreated"

echo
echo "== 3. Fill calendars (read-only, no email) =="
IPS=$(docker compose run --rm -T --entrypoint python nvr-report -c \
    "from app.config import load_inventory
for n in load_inventory('config/nvrs.local.json'): print(n.ip)" 2>/dev/null)
TODO=$IPS
for pass in 1 2 3 4; do
    echo "  Pass $pass"
    TOTAL_PENDING=0
    NEXT=""
    for ip in $TODO; do
        OUT=$(timeout 900 docker compose run --rm -T nvr-report --ip "$ip" 2>&1)
        ID=$(echo "$OUT" | sed -n 's/^Checking NVR \([^ ]*\) at.*/\1/p')
        CAL=$(echo "$OUT" | sed -n 's/^Recording calendar: //p')
        PENDING=$(echo "$CAL" | sed -n 's/.*; \([0-9]*\) pending;.*/\1/p')
        TOTAL_PENDING=$((TOTAL_PENDING + ${PENDING:-0}))
        # Only NVRs with calendars left (or no report) are checked again.
        [ "${PENDING:-1}" != "0" ] && NEXT="$NEXT $ip"
        printf '    %-7s %s\n' "${ID:-$ip}" "${CAL:-no report (NVR unreachable?)}"
    done
    echo "  Calendars still pending after pass $pass: $TOTAL_PENDING"
    [ -z "$NEXT" ] && break
    TODO=$NEXT
done

echo
if [ -z "$NEXT" ]; then
    echo "DONE: every reachable camera has a recording calendar."
else
    echo "DONE: $TOTAL_PENDING calendars still pending on:$NEXT (usually NVRs that timed out; the 08:00 run continues them)."
fi
echo "Refresh the dashboard. Log: $LOG"
sleep 1
