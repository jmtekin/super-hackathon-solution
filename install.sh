#!/bin/sh
# One Vultr VM (Ubuntu 24.04), as root, from this checkout: cp .env.example .env, fill it in, sh install.sh.
# Installs the console (ui/) and the BackupAirlock runtime (restoreguard/, 127.0.0.1:8600, reached only
# through the console's /rg/* proxy). With PUBLIC_HOST set, Caddy serves the console on https://PUBLIC_HOST/
# (automatic TLS) and the console itself listens on 127.0.0.1 only; without it the console is on port 80.
# Re-running is safe.
set -eu
cd "$(dirname "$0")"
[ -f .env ] || { echo "no .env: cp .env.example .env and fill it in" >&2; exit 1; }
set -a
. ./.env
set +a
RESTOREGUARD_HOST=127.0.0.1
if [ -n "${PUBLIC_HOST:-}" ]; then
  : "${WRITE_PASSWORD:?set WRITE_PASSWORD (basic auth for every write: Run drill, Add .sql, rerun)}"
  RESTORER_HOST=127.0.0.1
  RESTORER_PORT=8530
fi
export RESTOREGUARD_HOST RESTORER_HOST RESTORER_PORT
sh ui/install.sh
sh restoreguard/install.sh
[ -n "${PUBLIC_HOST:-}" ] || exit 0

apt-get -o DPkg::Lock::Timeout=600 install -y -qq caddy >/dev/null
# Reading is public; anything that is not a read starts work on this VM, so it needs the team password.
HASH="$(printf "%s\n" "$WRITE_PASSWORD" | caddy hash-password)"
umask 077
cat > /etc/caddy/Caddyfile <<CADDY
$PUBLIC_HOST {
	# never gzip the live event streams (SSE): gzip buffers them until the run ends
	@compress {
		not path_regexp (/events|/stream)\$
	}
	encode @compress gzip
	# no login (Jonas 16:47 "remove login"): judges can press Run; the runtime runs one at a time, only into test copies
	reverse_proxy 127.0.0.1:$RESTORER_PORT
}
CADDY
umask 022
chown root:caddy /etc/caddy/Caddyfile
chmod 640 /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
systemctl enable caddy >/dev/null
systemctl reload-or-restart caddy
# The first certificate needs an ACME round trip; give it up to two minutes.
i=0
until curl -fs "https://$PUBLIC_HOST/health"; do i=$((i + 1)); [ $i -lt 24 ] || exit 1; sleep 5; done
echo && echo "BackupAirlock is up: https://$PUBLIC_HOST/"
