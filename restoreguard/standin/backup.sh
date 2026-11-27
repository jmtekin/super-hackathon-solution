#!/bin/sh
# Backup job of the stand-in, per the contract: every BACKUP_EVERY seconds a
# pg_dump -Fc to $OUT/<UTC ts>_v<schema>.dump plus a manifest JSON next to it,
# keeping 48 h. The manifest's counts and sums are read in the dump's own
# snapshot (pg_export_snapshot), so live traffic can never make them disagree.
set -eu
OUT="${OUT:-/backups}"
EVERY="${BACKUP_EVERY:-1800}"
APP_URL="${APP_URL:-http://app:8000}"
export PGHOST="${PGHOST:-db}" PGUSER="${PGUSER:-nordvik}" PGDATABASE="${PGDATABASE:-nordvik}"

once() {
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  app_version=$(wget -qO- "$APP_URL/health" | sed -n 's/.*"app_version": *"\([^"]*\)".*/\1/p')
  tmp="$OUT/.partial-$ts"
  psql -X -q -At -v ON_ERROR_STOP=1 -v out="$tmp" <<SQL
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_export_snapshot() AS snap \gset
\setenv SNAP :snap
\! pg_dump -Fc --snapshot="\$SNAP" -f "$tmp.dump"
\o $tmp.stats
\i /contract/manifest.sql
\o
COMMIT;
SQL
  v=$(sed -n 's/.*"schema_version" : \([0-9]*\).*/\1/p' "$tmp.stats")
  name="${ts}_v${v}"
  mv "$tmp.dump" "$OUT/$name.dump"
  sha=$(sha256sum "$OUT/$name.dump" | cut -d' ' -f1)
  # stats is one JSON object; splice the file facts in front of its fields.
  printf '{"file": "%s.dump", "sha256": "%s", "created_at": "%s", "app_version": "%s", %s\n' \
    "$name" "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${app_version:-unknown}" "$(sed 's/^{//' "$tmp.stats")" \
    > "$OUT/.$name.json"
  mv "$OUT/.$name.json" "$OUT/$name.json"
  rm -f "$tmp.stats"
  find "$OUT" -maxdepth 1 -name '*_v*.dump' -mmin +2880 -exec sh -c 'rm -f "$1" "${1%.dump}.json"' _ {} \;
  echo "backup $name.dump $sha"
}

[ "${1:-loop}" = once ] && { once; exit; }
while true; do once || echo "backup failed" >&2; sleep "$EVERY"; done
