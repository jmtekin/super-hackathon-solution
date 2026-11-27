"""RestoreGuard's Frappe target: proves a Frappe site backup restores, and never writes to production.

Abdullah's ERP (Frappe 16 + ERPNext + demo_app, doctype Demo Service Request, MariaDB) runs two sites on
one Vultr VM: demo.local on port 80 is production, test.local on 8080 a DB-isolated clone. Jonas 13:55:
"you cant restore into prod". Production is only read: its Backups page lists the `bench backup` files,
/backups/<file> downloads one, get_count gives the source counts that become the manifest. Site refuses to
build a non-GET request to it, and a frappe.json whose test site could be production refuses to load.

Each backup goes through one cell:

  checksum -> gzip stream + static scan of the SQL -> full restore into a MariaDB 11.8 sandbox here
  (hardened like guard.Sandbox: no network, read-only root, data on tmpfs, 512 MB) -> row counts per
  doctype vs the source -> Demo Service Request rules, read from the dump's own DocField metadata ->
  test.local still answers -> its Demo Service Requests are replaced by the backup's through the token
  API, read back, compared and re-checked against the same rules

We have no shell on the VM, so `bench --site test.local restore` (the whole site) is out of reach: the
whole site is proved in the sandbox, the business doctype on test.local. A record test.local lacks has to
be created, and the app names it from its naming series (format:DSR-{YYYY}-{####}, not renamable); the
check says when that happened. A backup that fails a check before the replay never reaches test.local,
so the fault kit runs here only.
"""
import asyncio
import gzip
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

import guard

CONFIG = Path(os.environ.get("RESTOREGUARD_FRAPPE", guard.HERE / "frappe.json"))
DB_IMAGE = os.environ.get("RESTOREGUARD_MARIADB_IMAGE", "mariadb:11.8")
DB_MEMORY = "512m"  # the data dir is tmpfs inside this cap: a full ERPNext site is ~200 MB loaded
MIN_INTERVAL_S = 0.5  # one call to Abdullah's VM at a time, at most two a second
MAX_SQL_BYTES = 4 << 30
FILE = re.compile(r"^(\d{8}_\d{6}-[\w.-]+-database)\.sql\.gz$")
DOCTYPE = re.compile(r"^[\w .-]+$")
FIELD = re.compile(r"^\w+$")
LAYOUT = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading", "Fold", "Image"}
# What a site backup has no business containing: each writes or reads files, loads code or changes the
# server for whoever restores it. Only the SQL outside string literals is read: text in a record never
# runs, and Frappe's own records talk about loading data.
SCAN = [(re.compile(r"\bINTO\s+(OUT|DUMP)FILE\b", re.I), "writes a file on the database host"),
        (re.compile(r"\bLOAD\s+DATA\b|\bLOAD_FILE\s*\(", re.I), "reads a file on the database host"),
        (re.compile(r"\bSONAME\b", re.I), "loads native code into the server"),
        (re.compile(r"^\s*(\\!|system\s)", re.I), "mariadb client shell escape"),
        (re.compile(r"^\s*(GRANT\b|CREATE\s+USER\b|ALTER\s+USER\b|SET\s+GLOBAL\b)", re.I), "changes server accounts or settings"),
        (re.compile(r"\bCREATE\s+(DEFINER\s*=\s*\S+\s+)?(EVENT|TRIGGER)\b", re.I), "code that runs later inside the server")]
QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'")
_calls = threading.Lock()
_last_call = 0.0


def _hostport(url):
    u = urllib.parse.urlsplit(url)
    return (u.hostname or "").lower(), u.port or {"http": 80, "https": 443}.get(u.scheme)


def check_config(cfg):
    """Refuse a config that could restore into production (Jonas 13:55): the test site must be another
    site than the source, on another host:port, never on port 80 and never named demo.local."""
    src, test = cfg["source"], cfg["test"]
    host, port = _hostport(test["url"])
    for bad, why in [(port == 80, f"test target {host}:{port} is on port 80, production's port"),
                     ((host, port) == _hostport(src["url"]), f"test target {host}:{port} is the production host:port"),
                     (test["site"] in (src["site"], "demo.local"), f"test site {test['site']} is production")]:
        if bad:
            raise ValueError(f"frappe.json refused: {why}; restores go only into test.local")
    if not DOCTYPE.match(cfg["doctype"]):
        raise ValueError(f"frappe.json refused: doctype {cfg['doctype']!r}")
    return cfg


def config():
    return check_config(json.loads(CONFIG.read_text())) if CONFIG.exists() else None


class Site:
    """Token-API client for one site of the config. Only the test site may write."""

    def __init__(self, cfg, role):
        self.site, self.url = cfg[role]["site"], cfg[role]["url"].rstrip("/")
        self.writable = role == "test"
        self.token = json.loads(Path(cfg["tokens"]).read_text())[self.site]

    def call(self, path, method="GET", body=None, out=None, timeout=30):
        global _last_call
        if method != "GET" and not self.writable:
            raise PermissionError(f"{method} to {self.site} refused: production is read only")
        with _calls:
            time.sleep(max(0.0, _last_call + MIN_INTERVAL_S - time.monotonic()))
            _last_call = time.monotonic()
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"token {self.token}", "Accept": "application/json",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if out is None:
                    return r.status, r.read(), r.headers
                with open(out, "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                return r.status, b"", r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def json(self, path, **kw):
        status, body, _ = self.call(path, **kw)
        if not 200 <= status < 300:
            raise RuntimeError(f"{self.site} {kw.get('method', 'GET')} {path.split('?')[0]}: HTTP {status}")
        return json.loads(body) if body.strip() else {}

    def method(self, name, **args):
        q = urllib.parse.urlencode({k: v if isinstance(v, str) else json.dumps(v) for k, v in args.items()})
        return self.json(f"/api/method/{name}?{q}")["message"]

    def records(self, doctype, fields):
        q = urllib.parse.urlencode({"fields": json.dumps(["name", *fields]), "limit_page_length": 0})
        return self.json(f"/api/resource/{urllib.parse.quote(doctype)}?{q}")["data"]


def scan(path):
    """Stream the gzip once: (readable, doctypes with rows, static-scan findings, error)."""
    tables, findings, size, done = set(), [], 0, False
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for no, line in enumerate(f, 1):
                size += len(line)
                if size > MAX_SQL_BYTES:
                    return False, tables, findings, f"more than {MAX_SQL_BYTES >> 30} GB of SQL"
                if line.startswith("INSERT INTO `tab"):
                    tables.add(line[len("INSERT INTO `tab"):].split("`", 1)[0])
                done = done or line.startswith("-- Dump completed on")
                code = QUOTED.sub("''", line)
                for pattern, why in SCAN:
                    if pattern.search(code):
                        findings.append({"line": no, "text": line.strip()[:200], "why": why})
    except (OSError, EOFError, zlib.error) as e:
        return False, tables, findings, f"gzip: {e}"
    if not done:
        return False, tables, findings, "no 'Dump completed' trailer: the dump stops before mysqldump finished"
    return True, tables, findings, ""


def fetch_backups(cfg, dest):
    """Download production's new `bench backup` files, read only, and write each manifest last."""
    src = Site(cfg, "source")
    page = src.json("/api/method/frappe.desk.desk_page.getpage?name=backups")["docs"][0]["script"]
    dest.mkdir(parents=True, exist_ok=True)
    made = []
    for name in sorted(set(re.findall(r"/backups/([\w.-]+)\"", page))):
        m = FILE.match(name)
        if not m or (dest / f"{m[1]}.json").exists():
            continue
        part = dest / f".{name}.part"
        status, _, hdr = src.call(f"/backups/{urllib.parse.quote(name)}", out=part, timeout=600)
        if status != 200:
            raise RuntimeError(f"{src.site} /backups/{name}: HTTP {status}")
        ok, tables, _, err = scan(part)
        if not ok:
            raise RuntimeError(f"{name} unreadable as downloaded: {err}")
        q = urllib.parse.urlencode({"filters": json.dumps([["istable", "=", 0], ["issingle", "=", 0], ["is_virtual", "=", 0]]),
                                    "limit_page_length": 0})
        doctypes = {d["name"] for d in src.json(f"/api/resource/DocType?{q}")["data"]}
        counted = sorted(d for d in tables & doctypes - set(cfg.get("uncounted", [])) if DOCTYPE.match(d))
        versions = src.method("frappe.utils.change_log.get_versions")
        manifest = {"file": name, "sha256": guard.sha256_file(part), "site": src.site,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.strptime(hdr["Last-Modified"], "%a, %d %b %Y %H:%M:%S GMT")),
                    "schema_version": f"frappe {versions['frappe']['version']}",
                    "app_version": " + ".join(f"{k} {v['version']}" for k, v in versions.items() if k != "frappe"),
                    "row_counts": {d: src.method("frappe.client.get_count", doctype=d) for d in counted},
                    "counted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "counted_by": f"get_count on {src.site} right after the download, read only"}
        part.replace(dest / name)
        (dest / f"{m[1]}.json").write_text(json.dumps(manifest, indent=1))
        made.append(name)
    return made


def load_backup(gz):
    """A backup = <stem>.sql.gz + <stem>.json. Returns its record, or raises ValueError with the reason."""
    gz = Path(gz)
    stem = gz.name[:-len(".sql.gz")]
    mpath = gz.with_name(f"{stem}.json")
    if not mpath.exists():
        raise ValueError(f"no manifest next to {gz.name}")
    try:
        manifest = json.loads(mpath.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"manifest unreadable: {e}") from None
    if missing := {"file", "sha256", "site", "schema_version", "row_counts"} - set(manifest):
        raise ValueError(f"manifest lacks {', '.join(sorted(missing))}")
    if bad := [d for d in manifest["row_counts"] if not DOCTYPE.match(d)]:
        raise ValueError(f"manifest names unsafe doctypes: {bad[:3]}")
    return {"id": stem, "kind": "frappe", "file": gz.name, "path": str(gz), "bytes": gz.stat().st_size,
            "mtime": gz.stat().st_mtime, "manifest": manifest}


def target(cfg):
    """The one 'app version' column a Frappe backup is proved on: the configured test site."""
    return {"version": cfg["test"]["site"], "image": DB_IMAGE, "label": f"{cfg['test']['site']} (Frappe)", "cfg": cfg}


def policy():
    return {"runtime": "docker (hardened)", "image": DB_IMAGE, "network": "none", "cpus": "1", "cpu_shares": 256,
            "memory": DB_MEMORY, "pids_limit": 128, "rootfs": "read-only", "input": "read-only",
            "data": "tmpfs 400 MB", "capabilities": "all dropped", "user": "999 (mysql)", "timeout_s": guard.CELL_TIMEOUT}


class FrappeCell(guard.Cell):
    def restored_into(self):
        cfg = self.app["cfg"]
        host, port = _hostport(cfg["test"]["url"])
        return {"instance": f"{cfg['test']['site']} on {host}:{port}, production untouched",
                "database": f"MariaDB 11.8 sandbox {self.name}, network none (full site restore)",
                "app": f"{cfg['test']['site']}: {cfg['doctype']} records replayed through its token API",
                "production": f"{cfg['source']['site']}: read only"}


class DbSandbox(guard.Sandbox):
    """A MariaDB 11.8 container with no network, the backup at /input/backup.sql.gz (read-only)."""

    memory = DB_MEMORY

    async def __aenter__(self):
        gz = Path(self.cell.backup["path"]).resolve()
        args = ["run", "-d", "--name", self.name, "--network", "none", "--cpus", "1", "--cpu-shares", "256",
                "--memory", DB_MEMORY, "--memory-swap", DB_MEMORY, "--pids-limit", "128", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "999:999",
                "--tmpfs", "/var/lib/mysql:rw,size=400m,uid=999,gid=999,mode=0700",
                "--tmpfs", "/run/mysqld:rw,size=1m,uid=999,gid=999", "--tmpfs", "/tmp:rw,size=32m,uid=999,gid=999",
                "-e", "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1", "-e", "MARIADB_DATABASE=restored",
                "-v", f"{gz}:/input/backup.sql.gz:ro", DB_IMAGE, "--skip-networking", "--skip-log-bin",
                "--innodb-buffer-pool-size=32M", "--innodb-log-file-size=16M", "--performance-schema=OFF"]
        rc, _, err = await guard.docker(*args)
        if rc != 0:
            raise RuntimeError(f"sandbox did not start: {err.decode(errors='replace')[-300:]}")
        self.cell.emit({"type": "sandbox", "phase": "started", "name": self.name, "policy": policy(),
                        "restored_into": self.cell.restored_into()["instance"]})
        for _ in range(120):  # the entrypoint initialises a data dir and restarts once (~13 s here)
            if (await self.sql("SELECT 1"))[0] == 0:
                return self
            await asyncio.sleep(0.5)
        raise RuntimeError("sandbox MariaDB does not answer queries in 60 s")

    def sql(self, query, timeout=60):
        return self.exec("mariadb", "-uroot", "-N", "-B", "-r", "restored", stdin=query.encode(), timeout=timeout)


def _close(a, b):
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return a == b


def rules(fields, rows, existing):
    """Rules every Demo Service Request must meet: its doctype's own metadata (required fields, Select
    options, Link targets that exist in the same backup) plus the cost demo_app computes on save.
    fields: [{fieldname, fieldtype, options, reqd}]; existing: {link fieldname: names present}."""
    out = []
    for f in fields:
        name, kind, opts = f["fieldname"], f["fieldtype"], f.get("options") or ""
        if f.get("reqd"):
            bad = [r["name"] for r in rows if r.get(name) in (None, "")]
            out.append((f"required: {name}", not bad, f"missing in {', '.join(bad[:5])}" if bad else f"{len(rows)} rows"))
        if kind == "Select" and opts:
            allowed = set(opts.split("\n"))
            bad = [f"{r['name']}={r[name]!r}" for r in rows if r.get(name) not in (None, "") and r[name] not in allowed]
            out.append((f"{name} is one of {'/'.join(o for o in opts.split(chr(10)) if o)}", not bad, ", ".join(bad[:5])))
        if kind == "Link" and opts:
            bad = [f"{r['name']}->{r[name]}" for r in rows if r.get(name) and r[name] not in existing.get(name, set())]
            out.append((f"{name} links to an existing {opts}", not bad, ", ".join(bad[:5])))
    names = {f["fieldname"] for f in fields}
    if {"estimated_hours", "hourly_rate", "total_cost"} <= names:
        bad = [f"{r['name']}: {r['estimated_hours']} x {r['hourly_rate']} != {r['total_cost']}" for r in rows
               if not _close(float(r["estimated_hours"] or 0) * float(r["hourly_rate"] or 0), r["total_cost"] or 0)]
        out.append(("total_cost = estimated_hours x hourly_rate", not bad, "; ".join(bad[:5])))
        bad = [r["name"] for r in rows if float(r["estimated_hours"] or 0) <= 0 or float(r["hourly_rate"] or 0) < 0]
        out.append(("estimated_hours > 0, hourly_rate >= 0", not bad, ", ".join(bad[:5])))
    return out


def replay(cfg, fields, rows, before):
    """Make test.local's records of the doctype exactly the backup's. before = its records now, already
    in the event log (Frappe also keeps a Deleted Document of each one removed). Returns what changed."""
    test, doctype = Site(cfg, "test"), cfg["doctype"]
    path = f"/api/resource/{urllib.parse.quote(doctype)}"
    have = {d["name"]: d for d in before}
    want = {r["name"]: r for r in rows}
    done = {"removed": [have[n] for n in have if n not in want], "updated": [], "created": {}}
    for n in done["removed"]:
        test.json(f"{path}/{urllib.parse.quote(n['name'])}", method="DELETE")
    for n, r in want.items():
        body = {f: r.get(f) for f in fields}
        if n in have:
            test.json(f"{path}/{urllib.parse.quote(n)}", method="PUT", body=body)
            done["updated"].append(n)
        else:
            done["created"][n] = test.json(path, method="POST", body=body)["data"]["name"]
    return done


def compare(fields, rows, got, created):
    """Backup rows vs test.local's read-back, matched by name (or the name the app gave a re-created one)."""
    by_name, diffs = {d["name"]: d for d in got}, []
    for r in rows:
        d = by_name.get(created.get(r["name"], r["name"]))
        if d is None:
            diffs.append(f"{r['name']} missing")
            continue
        diffs += [f"{r['name']}.{f}: {r.get(f)!r} vs {d.get(f)!r}" for f in fields
                  if not _close(r.get(f), d.get(f)) and (r.get(f) or None) != (d.get(f) or None)]
    if (extra := len(got) - len(rows)) > 0:
        diffs.append(f"{extra} more records on test.local than in the backup")
    return diffs


def _fail(cell, why):
    cell.state, cell.reason = "failed", why


async def _cell(cell):
    b, m, cfg = cell.backup, cell.backup["manifest"], cell.app["cfg"]
    doctype = cfg["doctype"]
    t = time.monotonic()
    digest = await asyncio.to_thread(guard.sha256_file, b["path"])
    guard._timed(cell, "checksum", t)
    if not cell.check("sha256 matches manifest", digest == m["sha256"], f"file {digest[:16]}, manifest {m['sha256'][:16]}"):
        return _fail(cell, "checksum mismatch: the file is not the backup production wrote")
    t = time.monotonic()
    ok, _, cell.findings, err = await asyncio.to_thread(scan, b["path"])
    guard._timed(cell, "scan", t)
    cell.check("dump readable (gzip stream, mysqldump trailer)", ok, err)
    cell.check("static scan: no code paths in the dump", not cell.findings,
               "; ".join(f"line {f['line']}: {f['why']}" for f in cell.findings[:5]))
    if not ok:
        return _fail(cell, f"dump unreadable: {err}")
    async with DbSandbox(cell) as sb:
        t = time.monotonic()
        rc, _, err = await sb.exec("bash", "-o", "pipefail", "-c", "gunzip -c /input/backup.sql.gz | mariadb -uroot restored",
                                   timeout=150)
        guard._timed(cell, "restore", t)
        if not cell.check("restore completes", rc == 0, guard.airlock._last_line(err)):
            return _fail(cell, f"restore failed: {guard.airlock._last_line(err)}")
        if cell.findings:
            return  # detonated in the sandbox for the forensic log; run_cell quarantines it, test.local never sees it
        t = time.monotonic()
        _, out, _ = await sb.sql("SELECT table_name FROM information_schema.tables WHERE table_schema = 'restored'")
        present = {n[3:] for n in out.splitlines() if n.startswith("tab")}
        want = m["row_counts"]
        counted = [d for d in want if d in present]
        got = {}
        if counted:
            _, out, err = await sb.sql(" UNION ALL ".join(f"SELECT '{d}', COUNT(*) FROM `tab{d}`" for d in counted))
            got = {k: int(v) for k, v in (l.rsplit("\t", 1) for l in out.splitlines() if "\t" in l)}
        guard._timed(cell, "counts", t)
        off = [f"{d}: {got.get(d, 'no table')} vs source {n}" for d, n in want.items() if d != doctype and got.get(d) != n]
        cell.check(f"{doctype}: rows match the source", got.get(doctype) == want.get(doctype),
                   f"{got.get(doctype, 'no table')} restored, source {want.get(doctype)}")
        cell.check(f"row counts match the source ({len(want) - 1} other doctypes)", not off, "; ".join(off[:5]))
        rc, out, err = await sb.sql("SELECT JSON_OBJECT('fieldname', fieldname, 'fieldtype', fieldtype, 'options', options, "
                                    f"'reqd', reqd) FROM tabDocField WHERE parent = '{doctype}' ORDER BY idx")
        fields = [f for f in map(json.loads, out.splitlines()) if f["fieldtype"] not in LAYOUT and FIELD.match(f["fieldname"])]
        if not cell.check(f"{doctype} doctype present in the backup", rc == 0 and fields, guard.airlock._last_line(err)):
            return _fail(cell, f"{doctype} metadata missing from the backup")
        names = [f["fieldname"] for f in fields]
        cols = ", ".join(f"'{n}', `{n}`" for n in ["name", *names])
        _, out, _ = await sb.sql(f"SELECT JSON_OBJECT({cols}) FROM `tab{doctype}` ORDER BY name")
        rows = [json.loads(l) for l in out.splitlines()]
        existing = {}
        for f in fields:
            if f["fieldtype"] == "Link" and f["options"] and DOCTYPE.match(f["options"]):
                _, out, _ = await sb.sql(f"SELECT name FROM `tab{f['options']}`")
                existing[f["fieldname"]] = set(out.splitlines())
        for name, passed, detail in rules(fields, rows, existing):
            cell.check(f"rule: {name}", passed, detail)
    if failed := [c["check"] for c in cell.checks if not c["pass"]]:
        return _fail(cell, f"{failed[0]}; not restored into {cfg['test']['site']}")
    test = Site(cfg, "test")
    t = time.monotonic()
    user = await asyncio.to_thread(test.method, "frappe.auth.get_logged_user")
    if not cell.check(f"{cfg['test']['site']} app answers (get_logged_user)", user, f"user {user}"):
        return _fail(cell, f"{cfg['test']['site']} does not answer")
    before = await asyncio.to_thread(test.records, doctype, names)
    cell.emit({"type": "replay", "site": cfg["test"]["site"], "phase": "before", "records": before})
    done = await asyncio.to_thread(replay, cfg, names, rows, before)
    cell.emit({"type": "replay", "site": cfg["test"]["site"], "phase": "done", "updated": done["updated"],
               "created": done["created"], "removed": [d["name"] for d in done["removed"]]})
    got = await asyncio.to_thread(test.records, doctype, names)
    count = await asyncio.to_thread(test.method, "frappe.client.get_count", doctype=doctype)
    guard._timed(cell, "test_site", t)
    diffs = compare(names, rows, got, done["created"])
    renamed = {k: v for k, v in done["created"].items() if k != v}
    cell.check(f"{cfg['test']['site']} holds the backup's {len(rows)} {doctype} records", not diffs and count == len(rows),
               "; ".join(diffs[:5]) or f"{len(done['updated'])} updated, {len(done['created'])} created"
               + (f" (renamed by the naming series: {', '.join(f'{k}->{v}' for k, v in renamed.items())})" if renamed else "")
               + f", {len(done['removed'])} test-only removed; resource list readable, get_count {count}")
    for name, passed, detail in rules([f for f in fields if f["fieldtype"] != "Link"], got, {}):
        cell.check(f"on {cfg['test']['site']}: {name}", passed, detail)
    if failed := [c["check"] for c in cell.checks if not c["pass"]]:
        return _fail(cell, failed[0])
    cell.state, cell.reason = "native", f"restored: full site in the sandbox, {len(rows)} {doctype} records on {cfg['test']['site']}"


def run_cell(backup, app, emit=lambda ev: None):
    return guard.run_cell(backup, app, emit, cell_type=FrappeCell, body=_cell)
