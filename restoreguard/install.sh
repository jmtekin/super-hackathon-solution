#!/bin/sh
# One-command install of RestoreGuard on a fresh Vultr VM (Ubuntu 24.04), as root, with
# projects/restorer checked out next to this directory (RestoreGuard imports its LLM helpers):
#   VULTR_INFERENCE_KEY=... sh install.sh                      (watches ./backups, API on :8600)
#   VULTR_INFERENCE_KEY=... RESTOREGUARD_BACKUPS_URL=https://<app-vm>/backups/ BACKUPS_TOKEN=... sh install.sh
# Re-running is safe: it updates the code, rebuilds the stand-in images and restarts the service.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
: "${VULTR_INFERENCE_KEY:?set VULTR_INFERENCE_KEY (Vultr Serverless Inference API key)}"
SRC="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SRC/../ui/app.py" ] || { echo "ui/ (the console app) must sit next to restoreguard/" >&2; exit 1; }
DEST=/opt/restoreguard
PORT="${RESTOREGUARD_PORT:-8600}"

apt-get update -qq
apt-get install -y -qq docker.io python3-venv rsync >/dev/null
systemctl enable --now docker
id restoreguard >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin restoreguard
usermod -aG docker restoreguard
mkdir -p "$DEST/restoreguard" "$DEST/restorer"
rsync -a --delete --exclude .git --exclude state --exclude backups --exclude faults/out "$SRC"/ "$DEST/restoreguard"/
rsync -a --delete --exclude .git --exclude .venv --exclude runs --exclude shots "$SRC/../ui"/ "$DEST/restorer"/
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q -r "$DEST/restorer/requirements.txt"
docker pull -q postgres:16-alpine
# Stand-in app versions until Mohamed's images replace them in apps.json.
docker build -q -t nordvik-standin:1.0.0 --build-arg APP_VERSION=1.0.0 --build-arg SCHEMA=1 "$DEST/restoreguard/standin"
docker build -q -t nordvik-standin:2.0.0 --build-arg APP_VERSION=2.0.0 --build-arg SCHEMA=2 "$DEST/restoreguard/standin"
mkdir -p "$DEST/restoreguard/backups" "$DEST/restoreguard/state" /etc/restoreguard

# Secrets live only here and in the control-plane process, never in a sandbox.
umask 077
[ -n "${BACKUPS_TOKEN:-}" ] && printf '%s' "$BACKUPS_TOKEN" > /etc/restoreguard/backups-token
[ -n "${ADMIN_PASSWORD:-}" ] && printf '%s' "$ADMIN_PASSWORD" > /etc/restoreguard/admin-password
cat > /etc/restoreguard/env <<ENV
VULTR_INFERENCE_KEY=$VULTR_INFERENCE_KEY
RESTORER_DIR=$DEST/restorer
RESTOREGUARD_HOST=0.0.0.0
RESTOREGUARD_PORT=$PORT
RESTOREGUARD_BACKUPS=$DEST/restoreguard/backups
RESTOREGUARD_BACKUPS_URL=${RESTOREGUARD_BACKUPS_URL:-}
RESTOREGUARD_BACKUPS_TOKEN_FILE=/etc/restoreguard/backups-token
RESTOREGUARD_ADMIN_PASSWORD_FILE=/etc/restoreguard/admin-password
ENV
umask 022
chown -R restoreguard:restoreguard "$DEST" /etc/restoreguard

cat > /etc/systemd/system/restoreguard.service <<UNIT
[Unit]
Description=RestoreGuard runtime
After=network-online.target docker.service

[Service]
User=restoreguard
Group=restoreguard
WorkingDirectory=$DEST/restoreguard
EnvironmentFile=/etc/restoreguard/env
ExecStart=$DEST/.venv/bin/python api.py
Restart=always
MemoryMax=1G
Nice=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now restoreguard
systemctl restart restoreguard
sleep 3
curl -fsS "http://127.0.0.1:$PORT/health" && echo
