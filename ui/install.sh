#!/bin/sh
# One-command install of Restorer on a fresh Vultr VM (Ubuntu 24.04 / Debian 12), as root:
#   VULTR_INFERENCE_KEY=... sh install.sh            (serves on port 80)
#   VULTR_INFERENCE_KEY=... RESTORER_PORT=8530 sh install.sh
# Re-running is safe: it updates the code, rebuilds the sandbox image and restarts the service.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
: "${VULTR_INFERENCE_KEY:?set VULTR_INFERENCE_KEY (Vultr Serverless Inference API key)}"
PORT="${RESTORER_PORT:-80}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/restorer

apt-get -o DPkg::Lock::Timeout=600 update -qq
apt-get -o DPkg::Lock::Timeout=600 install -y -qq docker.io python3-venv rsync >/dev/null
systemctl enable --now docker

id restorer >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin restorer
usermod -aG docker restorer
mkdir -p "$DEST"
rsync -a --delete --exclude .venv --exclude runs --exclude .git --exclude shots "$SRC"/ "$DEST"/
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"
(cd "$DEST" && python3 fixtures/build.py)
mkdir -p "$DEST/runs"
chown -R restorer:restorer "$DEST"
docker build -q -t restorer-sandbox:2 -f "$DEST/sandbox.Dockerfile" "$DEST"

# The key lives only here and in the control-plane process, never in a sandbox.
umask 077
printf 'VULTR_INFERENCE_KEY=%s\nRESTORER_HOST=%s\nRESTORER_PORT=%s\n' "$VULTR_INFERENCE_KEY" "${RESTORER_HOST:-0.0.0.0}" "$PORT" > /etc/restorer.env
umask 022

cat > /etc/systemd/system/restorer.service <<UNIT
[Unit]
Description=Restorer control plane
After=network-online.target docker.service

[Service]
User=restorer
Group=restorer
SupplementaryGroups=docker
EnvironmentFile=/etc/restorer.env
WorkingDirectory=$DEST
ExecStart=$DEST/.venv/bin/python app.py
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
MemoryMax=1G
# Open live streams (SSE) keep uvicorn from exiting: without this a restart took the site down for 90 s (16:38).
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable restorer >/dev/null
systemctl restart restorer
[ "${RESTORER_HOST:-0.0.0.0}" = 127.0.0.1 ] || { command -v ufw >/dev/null && ufw status | grep -q active && ufw allow "$PORT/tcp" >/dev/null || true; }
sleep 2
curl -fsS "http://127.0.0.1:$PORT/health" && echo && echo "Console is up: http://$(hostname -I | awk '{print $1}'):$PORT/"
