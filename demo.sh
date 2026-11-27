#!/bin/sh
# Pitch demo (~3 min): drops four backups into the running RestoreGuard, one at a time, so the Backups tab shows
# each state in turn: a good backup (DR ready), a corrupted one (not restorable), a schema-drift one (GLM bridge
# heals it on app 1.0.0) and a poisoned one (quarantined). Each step waits for the verdict, then PAUSE seconds
# of talking time. Demo copies are named *-demo-*; real backups are never touched.
#   ./demo.sh          reset, then run the four steps
#   ./demo.sh reset    remove the demo copies and their results (the service keeps running)
# Not in parallel with e2e.sh or faults.py: one sandbox at a time on this box.
set -e
cd "$(dirname "$0")"
PY=${PY:-/opt/agent/projects/restorer/.venv/bin/python}
PAUSE=${PAUSE:-20}
ctl() { "$PY" e2e/rgctl.py "$@"; }
say() { echo "$(date +%H:%M:%S) +$(( $(date +%s) - T0 ))s  $*"; }

ctl reset demo
[ "$1" = reset ] && exit 0
T0=$(date +%s)
step() {
    kind=$1; shift
    say "$*"
    id=$(ctl drop "$kind" demo)
    say "  dropped $id"
    say "  $(ctl wait "$id" 300)"
}
step good "1/4 tonight's backup lands: restore into a test instance, check, boot both app versions"
sleep "$PAUSE"
step corrupted "2/4 a backup corrupted after it was signed: the restore itself fails"
sleep "$PAUSE"
step drift "3/4 a backup from the new schema: app 1.0.0 refuses it, GLM writes a bridge, checks pass again"
sleep "$PAUSE"
step poisoned "4/4 a poisoned dump that runs a shell command on restore: contained and quarantined"
say "done"
