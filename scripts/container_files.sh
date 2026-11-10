#!/usr/bin/env bash
# Give the containers' user (UID/GID 10001, see deploy/Dockerfile) what it needs:
#   reports/ and state/           owned by 10001 (the services write there)
#   config/nvrs.local.json        root, group 10001, 640 (read-only for the services)
#   config/tls/dashboard.key      root, group 10001, 640 (folder 750)
# .env stays root-only (600): Docker Compose reads it, no container mounts it.
# Changes only what differs and never follows symbolic links. Prints the number
# of files and folders whose owner changed.
#
# Run by scripts/deploy_check.sh. By hand (for example after restoring a backup):
#   cd /opt/NVR_Track && sudo bash scripts/container_files.sh
set -u

APP_DIR=${APP_DIR:-/opt/NVR_Track}
ID=10001
cd "$APP_DIR" || exit 1
STATUS=0

mkdir -p reports state/scan state/notes state/events state/snapshots state/telegram state/live \
    state/custom_reports || STATUS=1
CHANGED=$(set -o pipefail
          find reports state \( ! -uid "$ID" -o ! -gid "$ID" \) -print -exec chown -h "$ID:$ID" {} + | wc -l) \
    || STATUS=1

for FILE in config/nvrs.local.json config/tls/dashboard.key; do
    if [ -f "$FILE" ] && [ ! -L "$FILE" ]; then
        chown root:"$ID" "$FILE" && chmod 640 "$FILE" || STATUS=1
    fi
done
if [ -d config/tls ] && [ ! -L config/tls ]; then
    chown root:"$ID" config/tls && chmod 750 config/tls || STATUS=1
fi

echo "${CHANGED:-0}"
exit "$STATUS"
