#!/usr/bin/env bash
# Create (or renew) the dashboard's HTTPS certificate: self-signed, for the
# address in DASHBOARD_BIND_IP, valid 825 days. Prints only the certificate's
# fingerprint; never prints .env values.
#
#   cd /opt/NVR_Track && sudo bash scripts/dashboard_cert.sh            # first time
#   cd /opt/NVR_Track && sudo bash scripts/dashboard_cert.sh --renew    # replace it
#
# Then set DASHBOARD_TLS=true in .env and run:
#   docker compose up -d --force-recreate nvr-dashboard
set -eu

APP_DIR=${APP_DIR:-/opt/NVR_Track}
cd "$APP_DIR"
DIR=config/tls
CRT=$DIR/dashboard.crt
KEY=$DIR/dashboard.key
DAYS=825

command -v openssl >/dev/null || { echo "openssl is not installed (sudo apt install openssl)."; exit 1; }
# Only this one setting is read from .env.
IP=$(sed -n 's/^DASHBOARD_BIND_IP=//p' .env 2>/dev/null | tail -1 | tr -d "\"' \r")
if ! echo "$IP" | grep -qE '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || [ "$IP" = "0.0.0.0" ] || [ "$IP" = "127.0.0.1" ]; then
    echo "Set DASHBOARD_BIND_IP in .env to the server's LAN IP first (now: ${IP:-empty})."
    exit 1
fi
if [ -e "$CRT" ] && [ "${1:-}" != "--renew" ]; then
    echo "$CRT already exists ($(openssl x509 -in "$CRT" -noout -enddate | sed 's/notAfter=/valid until /'))."
    echo "To replace it: sudo bash scripts/dashboard_cert.sh --renew"
    exit 1
fi

mkdir -p "$DIR"
# The dashboard runs as UID/GID 10001: it reads the key through its group.
chown root:10001 "$DIR"
chmod 750 "$DIR"
umask 077
openssl req -x509 -newkey rsa:3072 -sha256 -days "$DAYS" -nodes \
    -keyout "$KEY.new" -out "$CRT.new" -subj "/CN=NVR Track dashboard" \
    -addext "subjectAltName=IP:$IP" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" 2>/dev/null
chown root:10001 "$KEY.new"
chmod 640 "$KEY.new"
chmod 644 "$CRT.new"
mv -f "$KEY.new" "$KEY"
mv -f "$CRT.new" "$CRT"

echo "Certificate for $IP saved in $DIR (valid $DAYS days)."
echo "Browsers show a warning once per browser. The fingerprint to compare in the browser:"
openssl x509 -in "$CRT" -noout -fingerprint -sha256 | sed 's/^sha256 Fingerprint=/  SHA-256: /I'
echo "Next: set DASHBOARD_TLS=true in .env, then: docker compose up -d --force-recreate nvr-dashboard"
