"""RestoreGuard's Frappe target: proves a Frappe site backup restores, and never writes to production.

Abdullah's ERP (Frappe 16 + ERPNext + demo_app, doctype Demo Service Request, MariaDB) runs on one Vultr
VM: demo.local on port 80 is production; each test site is one app-version column, test.local on 8080 (same
bench, demo_app 0.0.1) and test2.local on 8081 (second bench, demo_app 2.0.0). Jonas 13:55:
"you cant restore into prod". Production is only read: its Backups page lists the `bench backup` files,
/backups/<file> downloads one, get_count gives the source counts that become the manifest. Site refuses to
build a non-GET request to it, and a frappe.json with a test site that could be production refuses to load.

Each backup goes through one cell:

  checksum -> gzip stream + static scan of the SQL -> full restore into a MariaDB 11.8 sandbox here
  (hardened like guard.Sandbox: no network, read-only root, data on tmpfs, 512 MB) -> row counts per
  doctype vs the source -> Demo Service Request rules, read from the dump's own DocField metadata ->
  the real restore on the VM: `bench --site test.local restore` of the same file -> test.local's login
  page and token API answer -> its row counts per doctype vs the source -> its Demo Service Requests read
  back, compared with the backup's and re-checked against the same rules

The VM restore goes through a forced-command SSH key (Abdullah's restore_test_site.sh: site test.local
hardcoded, accepts only one demo.local backup file name, flock against overlapping restores), so this
runtime can restore test.local and nothing else there. Every restore drops and recreates test.local, so
the sandbox goes first: a backup that fails a check or carries code never reaches the VM, and the fault
kit runs here only.

A test site whose app has another Demo Service Request schema than the backup (demo_app 2.0.0 renamed
estimated_hours to effort_hours with no migration patch and added a required category) is drift: bench
migrate succeeds there but leaves the data wrong. The cell then restores natively once on the VM to show
what migrate alone left, and GLM writes a MariaDB bridge (UPDATEs of that doctype only) that is validated in
the sandbox against an emulation of migrate before the VM restores again with it: "healed". Proof of a
bridge is data, not a field mapping the model claims: every field the app dropped must have its values,
row for row, in one field the app added, and the app's own rules must hold.
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
MIN_INTERVAL_S = 0.5  # production serves real users: one call to it at a time, at most two a second
TEST_PARALLEL = 8  # the test sites are ours: get_count of ~135 doctypes 8 at a time (~2 s; serialised it was ~68 s)
RESTORE_TIMEOUT_S = 600  # bench restore + migrate of the whole site on the VM
# the sandbox, the VM's restore (twice on drift: native, then bridged), then the test site's checks
CELL_TIMEOUT = guard.CELL_TIMEOUT + 2 * RESTORE_TIMEOUT_S + 180
MAX_SQL_BYTES = 4 << 30
FILE = re.compile(r"^(\d{8}_\d{6}-[\w.-]+-database)\.sql\.gz$")
DOCTYPE = re.compile(r"^[\w .-]+$")
FIELD = re.compile(r"^\w+$")
BRIDGE_STATEMENT = re.compile(r"^UPDATE\s+`tab([\w .-]+)`\s+SET\s", re.I)
# bench migrate adds a new docfield's column with these types and fills existing rows with the default
COLUMN = {"Float": "decimal(21,9) NOT NULL DEFAULT {d}", "Currency": "decimal(21,9) NOT NULL DEFAULT {d}",
          "Int": "int(11) NOT NULL DEFAULT {d}", "Check": "int(1) NOT NULL DEFAULT {d}",
          "Small Text": "text", "Text": "longtext", "Long Text": "longtext"}
BRIDGE_SYSTEM = """You are BackupAirlock's bridge writer for a Frappe ERP on MariaDB. A backup made by an older version of the app was restored into a site running a newer version and bench migrate ran: migrate added the new fields' columns (filled with their defaults) and kept the old columns in the table, so the old values are still readable there. Write the SQL that puts the old data into the new fields.
- Only UPDATE statements on the one table named below, one per line, each ending with a semicolon. No DDL, no other tables, no comments, no placeholders.
- A field the new version renamed: copy the old column's value. A new required field: fill every row with one of its allowed options, derived from the row's data (title, notes) where it allows, else the most fitting single option.
- Do not change any other field.
Reply with a line starting CHANGES: followed by one short line per change, then the SQL in one ```sql block."""
BRIDGE_REPAIR_SYSTEM = """You are BackupAirlock's bridge repairer. The bridge SQL failed or the checks after it failed. Fix it with the smallest change. Same rules: UPDATE statements on that one table only.
Reply with a line starting CAUSE: (one sentence), then the full corrected SQL in one ```sql block."""
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


def columns(cfg):
    """One view of the config per test site, as the rest of this module reads it: cfg["test"], cfg["ssh"]."""
    return [{**cfg, "test": t, "ssh": t["ssh"]} for t in cfg["tests"]]


def check_column(cfg):
    """Refuse a test site that could be production (Jonas 13:55): another site than the source, on another
    host:port, never on port 80 and never named demo.local."""
    src, test = cfg["source"], cfg["test"]
    host, port = _hostport(test["url"])
    for bad, why in [(port == 80, f"test target {host}:{port} is on port 80, production's port"),
                     ((host, port) == _hostport(src["url"]), f"test target {host}:{port} is the production host:port"),
                     (test["site"] in (src["site"], "demo.local"), f"test site {test['site']} is production")]:
        if bad:
            raise ValueError(f"frappe.json refused: {why}; restores go only into test.local")
    if not DOCTYPE.match(cfg["doctype"]):
        raise ValueError(f"frappe.json refused: doctype {cfg['doctype']!r}")
    if cfg["ssh"]["login"].rsplit("@", 1)[-1].lower() != host:
        raise ValueError(f"frappe.json refused: ssh {cfg['ssh']['login']} is not the test site's host {host}")
    return cfg


def check_config(cfg):
    sites = [t["site"] for t in cfg["tests"]]
    if not sites or len(set(sites)) != len(sites):
        raise ValueError(f"frappe.json refused: test sites {sites}")
    for view in columns(cfg):
        check_column(view)
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
        if not self.writable:
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

    def fields(self, doctype):
        """The doctype's data fields as this site's app version defines them."""
        doc = self.json(f"/api/resource/DocType/{urllib.parse.quote(doctype)}")["data"]
        return [{k: f.get(k) for k in ("fieldname", "fieldtype", "options", "reqd", "default")} for f in doc["fields"]
                if f["fieldtype"] not in LAYOUT and FIELD.match(f["fieldname"])]

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


def vm_file(cfg, name):
    """The VM's restore script takes only production's own file names (exit 2 for anything else); a fault
    copy or a renamed file is refused here before it costs a test.local restore."""
    site = re.escape(cfg["source"]["site"].replace(".", "_"))
    return re.fullmatch(rf"\d{{8}}_\d{{6}}-{site}-database\.sql\.gz", name) is not None


async def vm_restore(cfg, name, bridge=None, on_line=lambda line: None):
    """`bench --site <test site> restore` of production's backup `name` + migrate, on the VM; with `bridge`,
    the forced command then runs that SQL in the test site's own database. bench's output goes to on_line as it
    comes. Returns (rc, last line)."""
    s = cfg["ssh"]
    return await guard._stream("ssh", "-F", "/dev/null", "-i", s["key"], "-o", "BatchMode=yes",
                               "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
                               "-o", "ServerAliveInterval=30", s["login"], name, *(["bridge"] if bridge else []),
                               on_line=on_line, stdin=bridge.encode() if bridge else None, timeout=RESTORE_TIMEOUT_S)


def targets(cfg):
    """The app-version columns a Frappe backup is proved on: one per test site."""
    return [{"version": c["test"]["site"], "image": DB_IMAGE, "label": f"{c['test']['site']} ({c['test']['app']})", "cfg": c}
            for c in columns(cfg)]


def policy():
    return {"runtime": "docker (hardened)", "image": DB_IMAGE, "network": "none", "cpus": "1", "cpu_shares": 256,
            "memory": DB_MEMORY, "pids_limit": 128, "rootfs": "read-only", "input": "read-only",
            "data": "tmpfs 400 MB", "capabilities": "all dropped", "user": "999 (mysql)", "timeout_s": CELL_TIMEOUT}


class FrappeCell(guard.Cell):
    def restored_into(self):
        cfg = self.app["cfg"]
        host, port = _hostport(cfg["test"]["url"])
        return {"instance": f"{cfg['test']['site']} on Vultr VM {host} (full site restore via bench)",
                "database": f"MariaDB 11.8 sandbox {self.name}, network none: the same backup, restored here first",
                "app": f"{cfg['test']['site']} on {host}:{port}: restored by the VM's forced-command script, checked through its token API",
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
                        "restored_into": self.cell.restored_into()["database"]})
        for _ in range(120):  # the entrypoint initialises a data dir and restarts once (~13 s); polled quietly
            if (await guard.docker("exec", self.name, "mariadb", "-uroot", "-e", "SELECT 1", "restored"))[0] == 0:
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


def rules(fields, rows, existing, cost):
    """Rules every Demo Service Request must meet: its doctype's own metadata (required fields, Select
    options, Link targets that exist in the same backup) plus the cost demo_app computes on save.
    fields: [{fieldname, fieldtype, options, reqd}]; existing: {link fieldname: names present};
    cost: [hours, rate, total] field names of that app version."""
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
    hours, rate, total = cost
    if {hours, rate, total} <= {f["fieldname"] for f in fields}:
        bad = [f"{r['name']}: {r[hours]} x {r[rate]} != {r[total]}" for r in rows
               if not _close(float(r[hours] or 0) * float(r[rate] or 0), r[total] or 0)]
        out.append((f"{total} = {hours} x {rate}", not bad, "; ".join(bad[:5])))
        bad = [r["name"] for r in rows if float(r[hours] or 0) <= 0 or float(r[rate] or 0) < 0]
        out.append((f"{hours} > 0, {rate} >= 0", not bad, ", ".join(bad[:5])))
    return out


def compare(fields, rows, got):
    """Backup rows vs test.local's read-back, matched by name."""
    by_name, diffs = {d["name"]: d for d in got}, []
    for r in rows:
        d = by_name.get(r["name"])
        if d is None:
            diffs.append(f"{r['name']} missing")
            continue
        diffs += [f"{r['name']}.{f}: {r.get(f)!r} vs {d.get(f)!r}" for f in fields
                  if not _close(r.get(f), d.get(f)) and (r.get(f) or None) != (d.get(f) or None)]
    if (extra := len(got) - len(rows)) > 0:
        diffs.append(f"{extra} more records on test.local than in the backup")
    return diffs


def carried(fields, rows, want, got):
    """Backup rows vs rows under the app's fields `want`: shared fields unchanged, and every field the app
    dropped has its values, row for row, in one field the app added. Returns ({old: new}, problems)."""
    have, names = [f["fieldname"] for f in fields], [f["fieldname"] for f in want]
    by_name, mapping, problems = {d["name"]: d for d in got}, {}, []
    for old in (n for n in have if n not in names):
        new = next((n for n in names if n not in have and all(
            r["name"] in by_name and _close(r.get(old), by_name[r["name"]].get(n)) for r in rows)), None)
        if new:
            mapping[old] = new
        else:
            problems.append(f"{old}: its values ({', '.join(str(r.get(old)) for r in rows[:3])}) are in no field of the new version")
    return mapping, problems + compare([n for n in have if n in names], rows, got)


def check_bridge(sql, doctype):
    """Policy for model-written SQL that later runs on the VM: UPDATEs of the one doctype's table, nothing else."""
    code = QUOTED.sub("''", sql)
    statements = [st.strip() for st in code.split(";") if st.strip()]
    if not statements:
        return "no statements"
    for st in statements:
        m = BRIDGE_STATEMENT.match(st)
        if not m or m[1] != doctype or "`tab" in st[m.end():] or re.search(r"\bSELECT\b", st, re.I):
            return f"refused by policy (only UPDATE `tab{doctype}` SET ...): {st[:80]}"
    for pattern, why in SCAN:
        if pattern.search(code):
            return f"refused by policy: {why}"
    return ""


def _fail(cell, why):
    cell.state, cell.reason = "failed", why


async def _cell(cell):
    b, m, cfg = cell.backup, cell.backup["manifest"], cell.app["cfg"]
    doctype = cfg["doctype"]
    cell.step("Verify the file")
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
    test = Site(cfg, "test")
    app_fields = await asyncio.to_thread(test.fields, doctype)
    cell.step("Start the MariaDB sandbox")
    async with DbSandbox(cell) as sb:
        cell.step("Restore into the sandbox")
        t = time.monotonic()
        rc, _, err = await _sandbox_restore(cell, sb)
        guard._timed(cell, "restore", t)
        if not cell.check("restore completes", rc == 0, guard.airlock._last_line(err)):
            return _fail(cell, f"restore failed: {guard.airlock._last_line(err)}")
        if cell.findings:
            return  # detonated in the sandbox for the forensic log; run_cell quarantines it, test.local never sees it
        cell.step("Check the sandbox copy")
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
        for name, passed, detail in rules(fields, rows, existing, cfg["source"]["cost"]):
            cell.check(f"rule: {name}", passed, detail)
        if failed := [c["check"] for c in cell.checks if not c["pass"]]:
            return _fail(cell, f"{failed[0]}; not restored into {cfg['test']['site']}")
        sql = None
        if {f["fieldname"] for f in app_fields} != set(names):  # drift: the sandbox stays up for the bridge
            if not cell.check(f"a {cfg['source']['site']} backup file name (the only kind the VM restores)", vm_file(cfg, b["file"]), b["file"]):
                return _fail(cell, f"{b['file']} is not a production backup name; not restored into {cfg['test']['site']}")
            native = await _native(cell, fields, rows, app_fields)
            if not cell.app.get("heal", True):
                return _fail(cell, f"{cfg['test']['app']} changed the {cfg['doctype']} fields and healing is off; not restored into {cfg['test']['site']}")
            if (sql := await _bridge(cell, sb, fields, rows, app_fields, native)) is None:
                return
    await _vm(cell, fields, rows, app_fields, sql)


async def _sandbox_restore(cell, sb):
    """gunzip | mariadb prints nothing until it ends (~20 s for a full site): the tables loaded so far are the progress line."""
    job = asyncio.ensure_future(sb.exec("bash", "-o", "pipefail", "-c", "gunzip -c /input/backup.sql.gz | mariadb -uroot restored",
                                        timeout=150))
    seen = 0
    while not (await asyncio.wait({job}, timeout=2))[0]:
        _, out, _ = await guard.docker("exec", sb.name, "mariadb", "-uroot", "-N", "-B", "-e",
                                       "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'restored'")
        n = int(out) if out.strip().isdigit() else 0
        if n > seen:
            seen = n
            cell.emit({"type": "log", "stream": "sandbox", "line": f"{n} tables loaded"})
    return job.result()


def _vm_lines(cell, site):
    """bench's lines into the run log as they come; a progress bar redrawn with \\r is passed on at most once a
    second, and its migrate phase gets its own step."""
    last = {"key": None, "at": 0.0, "migrate": False}

    def on_line(line):
        if not last["migrate"] and "migrat" in line.lower():
            last["migrate"] = True
            cell.step(f"Migrate {site}")
        key, now = line.split("[")[0].split(":")[0], time.monotonic()
        if key == last["key"] and now - last["at"] < 1:
            return
        last.update(key=key, at=now)
        cell.emit({"type": "log", "stream": "vm", "line": line})
    return on_line


async def _restore_on_vm(cell, bridge=None):
    b, cfg = cell.backup, cell.app["cfg"]
    site = cfg["test"]["site"]
    cell.step(f"Restore {site} on the VM" + (" with the bridge" if bridge else ""))
    t = time.monotonic()
    cell.emit({"type": "restore", "phase": "started", "file": b["file"], "bridge": bool(bridge),
               "restored_into": cell.restored_into()["instance"]})
    rc, last = await vm_restore(cfg, b["file"], bridge and f"START TRANSACTION;\n{bridge}\nCOMMIT;\n", _vm_lines(cell, site))
    guard._timed(cell, "vm_restore_bridged" if bridge else "vm_restore", t)
    cell.emit({"type": "restore", "phase": "done", "rc": rc, "s": round(time.monotonic() - t, 2)})
    return rc, last


async def _native(cell, fields, rows, want):
    """Drift: what bench restore + migrate alone leaves on the test site. Evidence for the bridge, not a check."""
    cfg = cell.app["cfg"]
    rc, last = await _restore_on_vm(cell)
    if rc != 0:
        problems = [f"bench restore + migrate failed (exit {rc}): {last}"]
    else:
        got = await asyncio.to_thread(Site(cfg, "test").records, cfg["doctype"], [f["fieldname"] for f in want])
        problems = carried(fields, rows, want, got)[1] + [
            f"{name}: {detail}" for name, ok, detail in rules([f for f in want if f["fieldtype"] != "Link"], got, {}, cfg["test"]["cost"]) if not ok]
    cell.emit({"type": "native", "site": cfg["test"]["site"], "problems": problems})
    return problems


async def _migrate_like_bench(sb, doctype, want):
    """In the sandbox, the columns bench migrate adds for the app's new fields, so a bridge runs on the same
    table shape as on the VM (old columns kept, new ones filled with their default)."""
    _, out, _ = await sb.sql(f"SELECT column_name FROM information_schema.columns WHERE table_name = 'tab{doctype}'")
    present = set(out.splitlines())
    for f in (f for f in want if f["fieldname"] not in present):
        d = f.get("default")
        kind = COLUMN.get(f["fieldtype"], "varchar(140) DEFAULT {s}")
        col = kind.format(d=float(d or 0) if "{d}" in kind else "", s="NULL" if d in (None, "") else "'" + str(d).replace("'", "''") + "'")
        rc, _, err = await sb.sql(f"ALTER TABLE `tab{doctype}` ADD COLUMN `{f['fieldname']}` {col}")
        if rc != 0:
            raise RuntimeError(f"sandbox migrate of {f['fieldname']}: {guard.airlock._last_line(err)}")


async def _bridge(cell, sb, fields, rows, want, native):
    """GLM writes the bridge, the sandbox proves it. Returns the SQL, or None with the cell failed."""
    cfg = cell.app["cfg"]
    doctype, names = cfg["doctype"], [f["fieldname"] for f in want]
    await _migrate_like_bench(sb, doctype, want)
    prompt = (f"Table: `tab{doctype}` (doctype {doctype}).\n"
              f"Fields in the backup's app version: {json.dumps(fields)}\n"
              f"Fields in the new app version ({cfg['test']['app']}): {json.dumps(want)}\n"
              f"After bench migrate alone: {json.dumps(native)}\n"
              f"Backup rows (up to 20): {json.dumps(rows[:20], default=str)[:6000]}")
    cell.step("Heal: the model writes a schema bridge")
    reply = await guard.airlock.llm(cell, "bridge", BRIDGE_SYSTEM, prompt)
    for rnd in range(1, guard.MAX_BRIDGE_ROUNDS + 1):
        sql = guard.airlock._script(reply)
        name = f"{cell.backup['id']}-to-{cfg['test']['site']}-r{rnd}.sql"
        bridge = {"name": name, "round": rnd, "sha256": guard.airlock._sha(sql), "sql": sql,
                  "changes": guard.airlock._section(reply, "CHANGES") or guard.airlock._section(reply, "CAUSE")}
        cell.bridges.append(bridge)
        cell.emit({"type": "bridge", "round": rnd, "name": name, "changes": bridge["changes"]})
        failure = check_bridge(sql, doctype)
        if not failure:
            # one transaction, rolled back unless every statement passes, so a failed round leaves the table as migrate left it
            rc, _, err = await sb.sql(f"START TRANSACTION;\n{sql}\nCOMMIT;")
            failure = "" if rc == 0 else f"bridge SQL failed: {guard.airlock._last_line(err)}"
        if not failure:
            cols = ", ".join(f"'{n}', `{n}`" for n in ["name", *names])
            _, out, _ = await sb.sql(f"SELECT JSON_OBJECT({cols}) FROM `tab{doctype}` ORDER BY name")
            got = [json.loads(l) for l in out.splitlines()]
            mapping, problems = carried(fields, rows, want, got)
            problems += [f"{n}: {d}" for n, ok, d in rules([f for f in want if f["fieldtype"] != "Link"], got, {}, cfg["test"]["cost"]) if not ok]
            if not problems:
                bridge["mapping"] = mapping
                cell.check(f"bridge round {rnd} proved in the sandbox", True,
                           ", ".join(f"{o} -> {n}" for o, n in mapping.items()) or "values carried")
                return sql
            failure = "; ".join(problems[:5])
        bridge["failure"] = failure
        cell.emit({"type": "bridge", "round": rnd, "name": name, "failure": failure})
        if rnd == guard.MAX_BRIDGE_ROUNDS:
            break
        # a rejected round may have committed: restore the backup again and redo migrate's columns
        await sb.exec("bash", "-o", "pipefail", "-c", "mariadb -uroot -e 'DROP DATABASE restored; CREATE DATABASE restored' && "
                      "gunzip -c /input/backup.sql.gz | mariadb -uroot restored", timeout=150)
        await _migrate_like_bench(sb, doctype, want)
        reply = await guard.airlock.llm(cell, "bridge repair", BRIDGE_REPAIR_SYSTEM,
                                        f"{prompt}\n\nPrevious SQL:\n{sql}\n\nFailure: {failure}")
    _fail(cell, f"no working bridge to {cfg['test']['site']} after {guard.MAX_BRIDGE_ROUNDS} rounds: {cell.bridges[-1].get('failure')}")


SHOT_PY = os.environ.get("RESTOREGUARD_SHOT_PY", "/opt/restoreguard/shot-venv/bin/python")
SHOTS = guard.STATE / "shots"


def shot_path(backup_id, site):
    return SHOTS / f"{backup_id}@{site}.png"


async def _shot(cell, test):
    """The restored site as it looks right now, for the console; a picture, never a check, so a failure is only logged."""
    cfg = cell.app["cfg"]
    site = cfg["test"]["site"]
    out = shot_path(cell.backup["id"], site)
    if not os.path.exists(SHOT_PY):
        return cell.emit({"type": "log", "line": f"no screenshot of {site}: {SHOT_PY} missing"})
    SHOTS.mkdir(parents=True, exist_ok=True)
    route = "app/" + cfg["doctype"].lower().replace(" ", "-")
    proc = await asyncio.create_subprocess_exec(SHOT_PY, str(Path(__file__).resolve().parent / "erp_shot.py"), cfg["test"]["url"], route, str(out),
                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        log, _ = await asyncio.wait_for(proc.communicate(test.token.encode()), 90)
    except asyncio.TimeoutError:
        proc.kill()
        log = b"timed out after 90 s"
    if proc.returncode == 0 and out.exists():
        return cell.emit({"type": "log", "line": f"screenshot of {site}/{route}"})
    last = (log.decode(errors="replace").strip().splitlines() or [f"exit {proc.returncode}"])[-1]
    cell.emit({"type": "log", "line": f"no screenshot of {site}: {last}"[:300]})


async def _vm(cell, fields, rows, want, bridge=None):
    """The sandbox passed (with its bridge, if the app drifted): restore the same file into the test site
    on the VM, then check it there."""
    b, m, cfg = cell.backup, cell.backup["manifest"], cell.app["cfg"]
    site, doctype = cfg["test"]["site"], cfg["doctype"]
    if not bridge and not cell.check(f"a {cfg['source']['site']} backup file name (the only kind the VM restores)",
                                     vm_file(cfg, b["file"]), b["file"]):
        return _fail(cell, f"{b['file']} is not a production backup name; not restored into {site}")
    rc, last = await _restore_on_vm(cell, bridge)
    how = "bench restore + migrate + bridge" if bridge else "bench restore + migrate"
    if not cell.check(f"{site} restored on the VM ({how})", rc == 0, f"exit {rc}: {last}" if rc else last):
        return _fail(cell, f"restore on the VM failed (exit {rc}); {site} may be part restored")
    test = Site(cfg, "test")
    cell.step(f"Check {site}")
    t = time.monotonic()
    status = (await asyncio.to_thread(test.call, "/login"))[0]
    cell.check(f"{site} login page answers", status == 200, f"HTTP {status}")
    user = await asyncio.to_thread(test.method, "frappe.auth.get_logged_user")
    if not cell.check(f"{site} token API answers (get_logged_user)", user, f"user {user}"):
        return _fail(cell, f"{site} does not answer after the restore")
    counts, gate = {}, asyncio.Semaphore(TEST_PARALLEL)

    async def count(d):
        async with gate:
            counts[d] = await asyncio.to_thread(test.method, "frappe.client.get_count", doctype=d)
        if len(counts) % 45 == 0 or len(counts) == len(m["row_counts"]):
            cell.emit({"type": "log", "stream": site, "line": f"get_count: {len(counts)} of {len(m['row_counts'])} doctypes counted"})
    await asyncio.gather(*map(count, m["row_counts"]))
    off = [f"{d}: {counts[d]} vs source {n}" for d, n in m["row_counts"].items() if counts[d] != n]
    cell.check(f"on {site}: row counts match the source ({len(counts)} doctypes)", not off,
               "; ".join(off[:5]) or f"get_count on {site} vs {cfg['source']['site']} at download")
    got = await asyncio.to_thread(test.records, doctype, [f["fieldname"] for f in want])
    guard._timed(cell, "test_site", t)
    mapping, diffs = carried(fields, rows, want, got)
    cell.check(f"{site} holds the backup's {len(rows)} {doctype} records", not diffs, "; ".join(diffs[:5]) or
               "read back, field by field" + "".join(f", {o} as {n}" for o, n in mapping.items()))
    for name, passed, detail in rules([f for f in want if f["fieldtype"] != "Link"], got, {}, cfg["test"]["cost"]):
        cell.check(f"on {site}: {name}", passed, detail)
    if failed := [c["check"] for c in cell.checks if not c["pass"]]:
        return _fail(cell, failed[0])
    cell.step(f"Screenshot {site}")
    await _shot(cell, test)
    if bridge:
        rnd = len(cell.bridges)
        cell.state, cell.reason = "healed", (f"{cfg['test']['app']}: migrate alone left the data wrong; GLM bridge round {rnd} "
                                             f"({', '.join(f'{o} -> {n}' for o, n in mapping.items())}) restored on {site}, {len(rows)} {doctype} records checked")
    else:
        cell.state, cell.reason = "native", f"restored: {site} on the VM is the backup (full site via bench), {len(rows)} {doctype} records checked"


def run_cell(backup, app, emit=lambda ev: None):
    return guard.run_cell(backup, app, emit, cell_type=FrappeCell, body=_cell, timeout=CELL_TIMEOUT)
