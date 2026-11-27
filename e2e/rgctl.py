"""Drop, watch and clean up RestoreGuard backups for the e2e suite and the pitch demo.

The runtime proves whatever lands in its watched folder, so both the tests and
the demo work the way production does: they copy a real backup (or one of the
fault kit's copies) under a fresh name into that folder and watch the API. The
copies carry a marker in their name ("-e2e-", "-demo-") so `reset` can remove
exactly them and nothing a real backup job wrote, without restarting the
service.

  python3 rgctl.py drop <good|corrupted|truncated|stale|rows-deleted|drift|poisoned> <marker>   -> prints the id
  python3 rgctl.py wait <id> [timeout_s]                                                        -> prints the result
  python3 rgctl.py reset <marker>
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RG = Path(os.environ.get("RG_HOME", Path(__file__).resolve().parents[2] / "restoreguard"))
URL = os.environ.get("RG_URL", "http://127.0.0.1:8600").rstrip("/")
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


def reset(marker, timeout=600):
    """Removes the marked copies once none of them is queued or running; the service drops a backup whose
    dump is gone at its next poll, so it keeps running (Jonas's UI stays up)."""
    t0 = time.monotonic()
    while any(b["status"] in ("queued", "running") for b in api("/api/backups") if f"-{marker}-" in b["id"]):
        if time.monotonic() - t0 > timeout:
            raise SystemExit(f"{marker} backups still queued or running after {timeout} s; not resetting")
        time.sleep(2)
    removed = 0
    for d in WATCHED + [RESULTS]:
        for f in d.glob(f"*-{marker}-*"):
            f.unlink()
            removed += 1
    while any(f"-{marker}-" in b["id"] for b in api("/api/backups")):
        if time.monotonic() - t0 > timeout:
            raise SystemExit(f"RestoreGuard still lists {marker} backups after {timeout} s")
        time.sleep(1)
    return removed


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "drop" and len(args) == 2 and args[0] in SOURCES:
        print(drop(*args))
    elif cmd == "wait" and args:
        b = wait(args[0], int(args[1]) if len(args) > 1 else 300)
        print(b["status"], f"{b['elapsed_s']} s", "|", b["reason"])
    elif cmd == "reset" and len(args) == 1:
        print(f"reset: {reset(args[0])} files removed")
    else:
        raise SystemExit(__doc__)
