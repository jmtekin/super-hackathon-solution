#!/bin/sh
# STAND-IN, this VPS only: no docker bridge, no compose and no long-running test
# servers here, so one short session makes real backups the way the compose
# stack would (seed, traffic, backup, upgrade to 2.0.0, traffic, backup) and
# removes everything. The Vultr VM runs docker-compose.yml instead. The current
# app.py/traffic.py are mounted over the image's copies, so no rebuild is needed.
#   sh standin/run_here.sh <backups dir> [scale]
set -eu
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
SCALE="${2:-1.0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
D="${RESTOREGUARD_DOCKER:-docker}"
ENVF="$(mktemp)"
trap '$D rm -f nordvik-app nordvik-db >/dev/null 2>&1; rm -f "$ENVF"' EXIT
printf 'ADMIN_PASSWORD=%s\nDATABASE_URL=postgresql://nordvik@127.0.0.1/nordvik\nEVENTS_PER_MIN=600\n' \
  "$(cat /opt/agent/.config/nordvik-standin-admin)" > "$ENVF"

$D run -d --name nordvik-db --network none --memory 256m --memory-swap 256m --cpus 1 \
  -e POSTGRES_USER=nordvik -e POSTGRES_HOST_AUTH_METHOD=trust \
  -v "$OUT:/backups" -v "$HERE/backup.sh:/standin/backup.sh:ro" -v "$HERE/../contract:/contract:ro" \
  postgres:16-alpine -c shared_buffers=32MB >/dev/null
until $D exec nordvik-db psql -U nordvik -c 'SELECT 1' >/dev/null 2>&1; do sleep 1; done
sleep 2; until $D exec nordvik-db psql -U nordvik -c 'SELECT 1' >/dev/null 2>&1; do sleep 1; done

app() {
  $D rm -f nordvik-app >/dev/null 2>&1 || true
  $D run -d --name nordvik-app --network container:nordvik-db --memory 128m --env-file "$ENVF" \
    -v "$HERE/app.py:/app/app.py:ro" -v "$HERE/traffic.py:/app/traffic.py:ro" "nordvik-standin:$1" >/dev/null
  until $D exec nordvik-db wget -qO- http://127.0.0.1:8000/health 2>/dev/null | grep -q '"ok":true'; do sleep 1; done
}
backup() {
  $D exec -e OUT=/backups -e APP_URL=http://127.0.0.1:8000 -e PGHOST=/var/run/postgresql nordvik-db sh /standin/backup.sh once
}

app 1.0.0
$D exec -i nordvik-db psql -X -q -U nordvik -v scale="$SCALE" < "$HERE/seed.sql"
$D exec nordvik-app timeout 20 python traffic.py || true
backup
app 2.0.0
$D exec nordvik-app timeout 20 python traffic.py || true
backup
$D exec nordvik-db chown -R "$(id -u):$(id -g)" /backups
