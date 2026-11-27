#!/bin/sh
# One Vultr VM (Ubuntu 24.04), as root, from this checkout: cp .env.example .env, fill it in, sh install.sh.
# Installs the console (ui/, public on RESTORER_PORT, default 80) and the RestoreGuard runtime
# (restoreguard/, 127.0.0.1:8600, reached only through the console's /rg/* proxy). Re-running is safe.
set -eu
cd "$(dirname "$0")"
[ -f .env ] || { echo "no .env: cp .env.example .env and fill it in" >&2; exit 1; }
set -a
. ./.env
set +a
RESTOREGUARD_HOST=127.0.0.1
export RESTOREGUARD_HOST
sh ui/install.sh
sh restoreguard/install.sh
