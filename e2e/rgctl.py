"""Drop, watch and clean up RestoreGuard backups for the e2e suite and the pitch demo.

The runtime proves whatever lands in its watched folder, so both the tests and
the demo work the way production does: they copy a real backup (or one of the
fault kit's copies) under a fresh name into that folder and watch the API. The
copies carry a marker in their name ("-e2e-", "-demo-") so `reset` can remove
exactly them and nothing a real backup job wrote. The service keeps backups in
memory, so reset has to restart it; it stops it first and clears any rg-
sandbox a killed cell left behind, and refuses while the fault kit holds the
box-wide sandbox lock.

  python3 rgctl.py drop <good|corrupted|truncated|stale|rows-deleted|drift|poisoned> <marker>   -> prints the id
  python3 rgctl.py wait <id> [timeout_s]                                                        -> prints the result
  python3 rgctl.py reset <marker>
"""
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RG = Path(os.environ.get("RG_HOME", Path(__file__).resolve().parents[2] / "restoreguard"))
URL = os.environ.get("RG_URL", "http://127.0.0.1:8600").rstrip("/")
UNIT = os.environ.get("RG_UNIT", "restoreguard")
DROP = RG / "backups"
WATCHED = [RG / "backups", RG / "faults" / "out"]
RESULTS = RG / "state" / "results"
# kind -> glob of its source; the fault kit's copies come from `python faults.py <backup>`
SOURCES = {"good": "backups/*Z_v1.dump", "corrupted": "faults/out/*Z-bitflip_v*.dump",
           "truncated": "faults/out/*Z-truncated_v*.dump", "stale": "faults/out/*Z-bitflip-stale_v*.dump",
           "rows-deleted": "faults/out/*Z-rows-deleted_v*.dump", "drift": "faults/out/*Z-schema-v2_v*.dump",
           "poisoned": "faults/out/*Z-poisoned_v*.dump"}
DONE = {"dr_ready", "not_restorable", "quarantined", "failed"}


def api(path, method="GET"):
    req = urllib.request.Request(URL + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def source(kind):
    found = sorted(RG.glob(SOURCES[kind]))
    if not found:
        raise SystemExit(f"no source for {kind}: {SOURCES[kind]} under {RG} (run faults.py on a good backup)")
    return found[-1]


def drop(kind, marker, manifest_edit=None, label=None):
    """Copy a source backup into the watched folder under a new id; manifest last, like the backup job."""
    src = source(kind)
    version = src.stem.rsplit("_v", 1)[1]
    stem = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{marker}-{label or kind}_v{version}"
    dump = DROP / f"{stem}.dump"
    tmp = DROP / f".{stem}.dump.part"
    tmp.write_bytes(src.read_bytes())
    tmp.replace(dump)
    m = dict(json.loads(src.with_suffix(".json").read_text()), file=dump.name)
    if manifest_edit:
        m = manifest_edit(m)
    dump.with_suffix(".json").write_text(m if isinstance(m, str) else json.dumps(m))
    return stem


def wait(bid, timeout=300):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            b = api(f"/api/backups/{bid}")
            if b["status"] in DONE:
                return b
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 = the watcher has not seen it yet
                raise
        time.sleep(1)
    raise TimeoutError(f"{bid} not finished after {timeout} s")


def reset(marker):
    lock = open(RG / "state" / "sandbox.lock", "a")
    subprocess.run(["sudo", "-n", "systemctl", "stop", UNIT], check=True)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        subprocess.run(["sudo", "-n", "systemctl", "start", UNIT], check=True)
        raise SystemExit("a sandbox is held by another process (faults.py?); not resetting")
    try:
        left = subprocess.run(["sudo", "-n", "docker", "ps", "-aq", "--filter", "name=^rg-"],
                              capture_output=True, text=True, check=True).stdout.split()
        if left:
            subprocess.run(["sudo", "-n", "docker", "rm", "-f", *left], capture_output=True, check=True)
        removed = 0
        for d in WATCHED + [RESULTS]:
            for f in d.glob(f"*-{marker}-*"):
                f.unlink()
                removed += 1
    finally:
        lock.close()
        subprocess.run(["sudo", "-n", "systemctl", "start", UNIT], check=True)
    for _ in range(60):
        try:
            api("/health")
            return removed, len(left)
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"{UNIT} did not come back on {URL}")


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "drop" and len(args) == 2 and args[0] in SOURCES:
        print(drop(*args))
    elif cmd == "wait" and args:
        b = wait(args[0], int(args[1]) if len(args) > 1 else 300)
        print(b["status"], f"{b['elapsed_s']} s", "|", b["reason"])
    elif cmd == "reset" and len(args) == 1:
        removed, containers = reset(args[0])
        print(f"reset: {removed} files removed, {containers} leftover sandboxes removed, {UNIT} up")
    else:
        raise SystemExit(__doc__)
