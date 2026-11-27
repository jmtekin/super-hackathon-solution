#!/bin/sh
# End-to-end suite against the running restoreguard.service (e2e/test_restoreguard.py, ~4 min).
# Not in parallel with demo.sh or faults.py: they share the one sandbox and the watched folder.
set -e
cd "$(dirname "$0")"
PY=${PY:-/opt/agent/projects/restorer/.venv/bin/python}
"$PY" e2e/rgctl.py reset e2e
rc=0
"$PY" -m pytest -q -p no:cacheprovider --durations=0 e2e "$@" || rc=$?
"$PY" e2e/rgctl.py reset e2e
exit $rc
