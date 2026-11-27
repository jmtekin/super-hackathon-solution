"""RestoreGuard runtime: proves every new ERP backup restores into a working app.

A backup is only worth something if it restores, the books in it still add up
and the app runs on it, so every backup the ERP's job writes (a pg_dump -Fc
plus a manifest, contract in projects/restorer/handoff/PROMPT-FOR-MOHAMED.md)
goes through the same cell for every known app version:

  checksum -> static scan -> restore in a fresh Postgres 16 sandbox -> row
  counts + invariant sums vs the manifest -> business rules -> boot the app
  image on the restored database and hit /health, /api/dashboard, login ->
  on a schema/app mismatch a GLM-5.3 bridge migration, repaired up to 3 rounds

The sandbox is hardened Docker like BackupAirlock's (projects/restorer,
whose LLM helpers and containment signals are reused here): no network, caps,
read-only root, the dump mounted read-only, data on a size-capped tmpfs, a
wall-clock limit, removed in every outcome and the removal checked. The app
joins the sandbox's network namespace, so it reaches the restored database on
loopback and still nothing leaves. The verdict comes only from sandbox
outcomes and the contract's own SQL (contract/), never from the model. One
sandbox runs at a time box-wide (a file lock), because this VPS has 8 GB and
other services on it.
"""
import asyncio
import fcntl
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("RESTORER_DIR", str(HERE.parent / "ui")))
import app as airlock  # noqa: E402  BackupAirlock: llm(), _script, _section, _plain, _sha
import sandbox as airlock_sandbox  # noqa: E402  containment signal patterns

DOCKER = os.environ.get("RESTOREGUARD_DOCKER", "docker").split()
PG_IMAGE = os.environ.get("RESTOREGUARD_PG_IMAGE", "postgres:16-alpine")
PG_MEMORY, APP_MEMORY = os.environ.get("RESTOREGUARD_PG_MEMORY", "384m"), "128m"
CELL_TIMEOUT = int(os.environ.get("RESTOREGUARD_TIMEOUT", "180"))
STATE = Path(os.environ.get("RESTOREGUARD_STATE", HERE / "state"))
ADMIN_EMAIL = "admin@nordvik.test"
ADMIN_PASSWORD_FILE = os.environ.get("RESTOREGUARD_ADMIN_PASSWORD_FILE", "/opt/agent/.config/nordvik-standin-admin")
MANIFEST_SQL = (HERE / "contract" / "manifest.sql").read_text()
RULES_SQL = (HERE / "contract" / "rules.sql").read_text()
MAX_BRIDGE_ROUNDS = 3
# What a pg_dump has no business containing: each runs code or touches files
# on whatever server restores it. Found before detonation, for the forensic log.
SCAN = [(re.compile(r"\b(TO|FROM)\s+PROGRAM\b", re.I), "COPY ... PROGRAM runs a shell command on the database host"),
        (re.compile(r"\bLANGUAGE\s+(c|plpython3?u|plperlu|pltclu)\b", re.I), "untrusted-language function, runs native code"),
        (re.compile(r"\b(pg_read_file|pg_read_binary_file|pg_ls_dir|lo_import|lo_export)\s*\(", re.I), "reads or writes server files"),
        (re.compile(r"\bCREATE\s+EVENT\s+TRIGGER\b|\bALTER\s+SYSTEM\b", re.I), "changes server-wide behaviour"),
        (re.compile(r"\bdblink|postgres_fdw\b", re.I), "opens connections out of the database")]
CONTAINED = {"network", "rootfs", "honey", "memory", "timeout"}
SIGNS = [(sig, pattern, event.replace("outside /work", "outside the database's data dir"))
         for sig, pattern, event in airlock_sandbox.SIGNS]

BRIDGE_SYSTEM = """You are RestoreGuard's bridge migration writer. A PostgreSQL 16 backup of an ERP was restored, but the ERP app version under test expects a different schema, so the app refuses to start on it. Write one SQL script that transforms the restored database in place into exactly the app's schema.
- It runs with psql -v ON_ERROR_STOP=1 in a single transaction on the restored database, as superuser, with no network.
- Keep every row. Derive new columns from the old data where the data allows; otherwise use the column default. Columns the app's schema no longer has are dropped only after their data is carried over.
- The schema_version table must end with the app's version as its max(version): delete rows above it, or insert it.
- Plain SQL only. No COPY, no PROGRAM, no functions, no comments, no placeholders.
Reply with a line starting CHANGES: followed by one short line per schema change, then the SQL in one ```sql block."""

BRIDGE_REPAIR_SYSTEM = """You are RestoreGuard's bridge migration repairer. A bridge migration failed or the app still refuses the database, or the contract checks failed after it. Fix it with the smallest change that addresses the error. Same rules: plain SQL, one transaction, keep every row.
Reply with a line starting CAUSE: (one sentence), then the full corrected SQL in one ```sql block."""


# The only database an app under test ever sees: the sandbox's own, on its loopback.
TEST_DSN = "postgresql://postgres@127.0.0.1:5432/restored"
APP_KEYS = {"version", "image", "schema", "port", "migrations", "label"}


def check_app(a):
    """Refuse an app entry that could point a restore anywhere but a throwaway test instance (Jonas 13:55:
    "you cant restore into prod"). The runtime has no setting for a target database at all; this
    makes sure nobody adds one through apps.json."""
    extra = set(a) - APP_KEYS
    if extra:
        raise ValueError(f"app {a.get('version')}: refused keys {', '.join(sorted(extra))}: restores go only into "
                         "a test instance RestoreGuard creates, never a configured database or host")
    if missing := APP_KEYS - {"label"} - set(a):
        raise ValueError(f"app {a.get('version')}: missing {', '.join(sorted(missing))}")
    for k, v in a.items():
        if isinstance(v, str) and re.search(r"postgres(ql)?://|\bhost=|@[\w.-]+:\d+/", v, re.I):
            raise ValueError(f"app {a['version']}: {k} looks like a database address; refused, restores go only "
                             "into a test instance")
    return a


def apps():
    return [check_app(a) for a in json.loads((HERE / "apps.json").read_text())]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def load_backup(dump):
    """A backup = <stem>.dump + <stem>.json. Returns its record, or raises ValueError with the reason."""
    dump = Path(dump)
    mpath = dump.with_suffix(".json")
    if not mpath.exists():
        raise ValueError(f"no manifest next to {dump.name}")
    try:
        manifest = json.loads(mpath.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"manifest unreadable: {e}") from None
    missing = {"file", "sha256", "schema_version", "row_counts", "invariants"} - set(manifest)
    if missing:
        raise ValueError(f"manifest lacks {', '.join(sorted(missing))}")
    return {"id": dump.stem, "file": dump.name, "path": str(dump), "bytes": dump.stat().st_size,
            "mtime": dump.stat().st_mtime, "manifest": manifest}


class Cell:
    """One (backup, app version) proof: events, checks, verdict. Duck-types airlock's run for llm()."""

    def __init__(self, backup, app, emit):
        self.id = f"{backup['id']}@{app['version']}"
        self.backup, self.app, self._emit = backup, app, emit
        self.t0, self.events, self.llm_calls, self.checks, self.bridges = time.monotonic(), [], [], [], []
        self.state, self.reason, self.signals, self.findings, self.timings = "running", "", set(), [], {}
        self.name = f"rg-{uuid.uuid4().hex[:10]}"

    def emit(self, ev):
        ev = {"cell": self.id, "backup": self.backup["id"], "app_version": self.app["version"],
              "t": round(time.monotonic() - self.t0, 2), **ev}
        self.events.append(ev)
        self._emit(ev)

    def check(self, name, ok, detail=""):
        self.checks.append({"check": name, "pass": bool(ok), "detail": detail})
        self.emit({"type": "check", "check": name, "pass": bool(ok), "detail": detail})
        return ok

    def restored_into(self):
        return {"instance": f"test instance {self.name}", "database": f"Postgres 16 sandbox {self.name}, network none",
                "app": f"{self.app.get('image')} as {self.name}-app, in the sandbox's network namespace",
                "dsn": TEST_DSN}

    def result(self):
        return {"cell": self.id, "backup": self.backup["id"], "app_version": self.app["version"],
                "restored_into": self.restored_into(), "state": self.state, "reason": self.reason, "checks": self.checks, "timings": self.timings,
                "signals": sorted(self.signals), "static_scan": self.findings, "bridges": self.bridges,
                "llm_calls": self.llm_calls, "elapsed_s": round(time.monotonic() - self.t0, 2), "events": self.events}


async def _proc(*args, stdin=None, timeout=60):
    p = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return "timeout", b"", b"timeout"
    return p.returncode, out, err


def docker(*args, **kw):
    return _proc(*DOCKER, *args, **kw)


class Sandbox:
    """A Postgres 16 container with no network, the dump at /input/backup.dump (read-only)."""

    memory = PG_MEMORY

    def __init__(self, cell):
        self.cell, self.name, self.app_name = cell, cell.name, cell.name + "-app"

    async def __aenter__(self):
        dump = Path(self.cell.backup["path"]).resolve()
        args = ["run", "-d", "--name", self.name, "--network", "none", "--cpus", "1", "--cpu-shares", "256",
                "--memory", PG_MEMORY, "--memory-swap", PG_MEMORY, "--pids-limit", "128", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "70:70",
                "--tmpfs", "/var/lib/postgresql/data:rw,size=200m,uid=70,gid=70,mode=0700",
                "--tmpfs", "/var/run/postgresql:rw,size=1m,uid=70,gid=70", "--tmpfs", "/tmp:rw,size=16m",
                "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "PGDATA=/var/lib/postgresql/data/pg",
                "-v", f"{dump}:/input/backup.dump:ro", PG_IMAGE,
                "-c", "shared_buffers=32MB", "-c", "fsync=off", "-c", "max_connections=20", "-c", "work_mem=8MB"]
        rc, out, err = await docker(*args)
        if rc != 0:
            raise RuntimeError(f"sandbox did not start: {err.decode(errors='replace')[-300:]}")
        self.cell.emit({"type": "sandbox", "phase": "started", "name": self.name, "policy": policy(),
                        "restored_into": self.cell.restored_into()["instance"]})
        for _ in range(60):
            rc, _, _ = await docker("exec", self.name, "pg_isready", "-q", "-h", "/var/run/postgresql")
            if rc == 0:
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("sandbox Postgres not ready in 30 s")
        # The entrypoint's init server restarts once; wait until a real query works.
        for _ in range(40):
            if (await self.psql("SELECT 1", db="postgres"))[0] == 0:
                return self
            await asyncio.sleep(0.5)
        raise RuntimeError("sandbox Postgres does not answer queries")

    async def __aexit__(self, *exc):
        for n in (self.app_name, self.name):
            await docker("rm", "-f", n)
        rc, _, _ = await docker("inspect", self.name)
        self.cell.emit({"type": "sandbox", "phase": "destroyed" if rc != 0 else "NOT DESTROYED", "name": self.name})

    async def exec(self, *cmd, stdin=None, timeout=120):
        rc, out, err = await docker("exec", *(["-i"] if stdin is not None else []), self.name, *cmd,
                                    stdin=stdin, timeout=timeout)
        text = err.decode(errors="replace")
        self._signals(text, "stderr")
        if rc == "timeout" and "timeout" not in self.cell.signals:
            self.cell.signals.add("timeout")
            self.cell.emit({"type": "policy", "signal": "timeout", "event": f"TIMEOUT: {timeout} s, sandbox killed"})
        if rc == 137 and "memory" not in self.cell.signals:
            self.cell.signals.add("memory")
            self.cell.emit({"type": "policy", "signal": "memory", "event": f"MEMORY CAP HIT: {self.memory}, process killed"})
        return rc, out.decode(errors="replace"), text

    def _signals(self, text, stream, keep=None):
        for line in text.splitlines():
            hits = [x for x in SIGNS if x[1].search(line)]
            if keep is None or hits or keep.search(line):
                self.cell.emit({"type": "log", "stream": stream, "line": line[:2000]})
            for sig, _, event in hits:
                if sig not in self.cell.signals:
                    self.cell.signals.add(sig)
                    self.cell.emit({"type": "policy", "signal": sig, "event": event, "detail": line[:300]})

    async def server_log(self):
        """Programs a dump starts (COPY ... TO PROGRAM) write to the server's log, not to pg_restore's stderr."""
        _, out, err = await docker("logs", self.name)
        self._signals((out + err).decode(errors="replace"), "server", keep=re.compile(r"ERROR|FATAL|PANIC"))

    def psql(self, sql, db="restored", timeout=60):
        return self.exec("psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-h", "/var/run/postgresql",
                         "-U", "postgres", "-d", db, "-1", stdin=sql.encode(), timeout=timeout)

    async def scan(self):
        """pg_restore -f - turns the dump into SQL text without executing it; read it for code paths."""
        rc, out, err = await self.exec("pg_restore", "-f", "-", "/input/backup.dump", timeout=60)
        findings = []
        for no, line in enumerate(out.splitlines(), 1):
            for pattern, why in SCAN:
                if pattern.search(line):
                    findings.append({"line": no, "text": line.strip()[:200], "why": why})
        return rc, findings, err

    async def wget(self, path, post=None, port=8000):
        """HTTP from inside the sandbox's namespace (busybox wget): the app is reachable on loopback only."""
        script = 'read -r body; if [ -n "$body" ]; then set -- --header "Content-Type: application/json" --post-data "$body"; fi; ' \
                 f'wget -q -T 5 -O - -S "$@" "http://127.0.0.1:{port}{path}" 2>&1'
        rc, out, _ = await self.exec("sh", "-c", script, stdin=((post or "") + "\n").encode(), timeout=15)
        status = re.findall(r"HTTP/1\.[01] (\d{3})", out)
        body = "\n".join(l for l in out.splitlines() if not l.startswith("  ") and "HTTP/" not in l)
        return (int(status[-1]) if status else None), body.strip()


def policy():
    return {"runtime": "docker (hardened)", "image": PG_IMAGE, "network": "none (app shares the sandbox's loopback)",
            "cpus": "1", "cpu_shares": 256, "memory": PG_MEMORY, "app_memory": APP_MEMORY, "pids_limit": 128,
            "rootfs": "read-only", "input": "read-only", "data": "tmpfs 200 MB", "capabilities": "all dropped",
            "user": "70 (postgres)", "timeout_s": CELL_TIMEOUT}


def _sandbox_lock():
    STATE.mkdir(parents=True, exist_ok=True)
    f = open(STATE / "sandbox.lock", "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


async def run_cell(backup, app, emit=lambda ev: None, cell_type=Cell, body=None):
    """Prove one backup on one app version. Never raises; the verdict is in the result.
    cell_type and body let another target (frappe_target) reuse the lock, timeout and quarantine rules."""
    cell = cell_type(backup, app, emit)
    lock = await asyncio.to_thread(_sandbox_lock)
    try:
        await asyncio.wait_for((body or _cell)(cell), CELL_TIMEOUT)
    except asyncio.TimeoutError:
        cell.signals.add("timeout")
        cell.state, cell.reason = "failed", f"cell exceeded {CELL_TIMEOUT} s"
    except Exception as e:
        cell.state, cell.reason = "failed", f"runtime error: {e}"[:300]
    finally:
        lock.close()
    if cell.signals & CONTAINED - {"timeout"} or cell.findings:
        why = ", ".join(sorted(cell.signals & CONTAINED)) or "static scan"
        cell.state, cell.reason = "quarantined", f"containment: {why}; {len(cell.findings)} static-scan finding(s)"
    cell.emit({"type": "verdict", "state": cell.state, "reason": cell.reason})
    return cell.result()


def _timed(cell, name, t):
    cell.timings[name] = round(time.monotonic() - t, 2)


async def _cell(cell):
    m, b = cell.backup["manifest"], cell.backup
    t = time.monotonic()
    digest = await asyncio.to_thread(sha256_file, b["path"])
    _timed(cell, "checksum", t)
    if not cell.check("sha256 matches manifest", digest == m["sha256"], f"file {digest[:16]}, manifest {m['sha256'][:16]}"):
        cell.state, cell.reason = "failed", "checksum mismatch: the dump is not the file the backup job wrote"
        return
    async with Sandbox(cell) as sb:
        t = time.monotonic()
        rc, cell.findings, err = await sb.scan()
        _timed(cell, "scan", t)
        cell.check("dump readable (pg_restore TOC + data)", rc == 0, airlock._last_line(err))
        cell.check("static scan: no code paths in the dump", not cell.findings,
                   "; ".join(f"line {f['line']}: {f['why']}" for f in cell.findings[:5]))
        if rc != 0:
            cell.state, cell.reason = "failed", f"dump unreadable: {airlock._last_line(err)}"
            return
        t = time.monotonic()
        await sb.exec("createdb", "-h", "/var/run/postgresql", "-U", "postgres", "restored")
        rc, _, err = await sb.exec("pg_restore", "--exit-on-error", "--no-owner", "-h", "/var/run/postgresql",
                                   "-U", "postgres", "-d", "restored", "/input/backup.dump", timeout=150)
        _timed(cell, "restore", t)
        await sb.server_log()
        if not cell.check("restore completes", rc == 0, airlock._last_line(err)):
            cell.state, cell.reason = "failed", f"restore failed: {airlock._last_line(err)}"
            return
        if not await _contract(cell, sb, "after restore"):
            return
        await _boot_and_bridge(cell, sb)


async def _contract(cell, sb, when, skip_counts=()):
    """Row counts + invariant sums vs the manifest, then the business rules. True if all hold."""
    m = cell.backup["manifest"]
    t = time.monotonic()
    rc, out, err = await sb.psql(MANIFEST_SQL)
    if not cell.check(f"manifest query runs ({when})", rc == 0, airlock._last_line(err)):
        cell.state, cell.reason = "failed", f"contract tables missing {when}: {airlock._last_line(err)}"
        return False
    got = json.loads(out)
    bad = [f"{t}: {got['row_counts'].get(t)} vs {n}" for t, n in m["row_counts"].items()
           if t not in skip_counts and got["row_counts"].get(t) != n]
    cell.check(f"row counts match manifest ({when})", not bad, "; ".join(bad) or f"{len(m['row_counts'])} tables")
    inv = [f"{k}: {got['invariants'].get(k)} vs {v}" for k, v in m["invariants"].items() if got["invariants"].get(k) != v]
    cell.check(f"invariant sums match manifest ({when})", not inv, "; ".join(inv) or ", ".join(m["invariants"]))
    rc, out, err = await sb.psql(RULES_SQL, timeout=90)
    _timed(cell, f"contract {when}", t)
    rules = json.loads(out) if rc == 0 else []
    broken = [f"{r['rule']}: {r['bad']} rows (e.g. id {r['example']})" for r in rules if r["bad"]]
    cell.check(f"business rules hold ({when})", rc == 0 and not broken,
               "; ".join(broken) if rc == 0 else airlock._last_line(err))
    if bad or inv or broken or rc != 0:
        cell.state = "failed"
        cell.reason = "; ".join(x for x in [bad and f"row counts differ ({bad[0]})", inv and f"sums differ ({inv[0]})",
                                             broken and f"rule broken ({broken[0]})",
                                             rc != 0 and "rules query failed"] if x)
        return False
    return True


async def _app_probe(cell, sb, when):
    """Start (or restart) the app on the restored db; returns (ok, error text)."""
    a = cell.app
    await docker("rm", "-f", sb.app_name)
    t = time.monotonic()
    rc, _, err = await docker("run", "-d", "--name", sb.app_name, "--network", f"container:{sb.name}",
                              "--memory", APP_MEMORY, "--memory-swap", APP_MEMORY, "--cpus", "1", "--pids-limit", "64",
                              "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                              "--tmpfs", "/tmp:rw,size=16m",
                              "-e", f"DATABASE_URL={TEST_DSN}", a["image"])
    if rc != 0:
        return False, f"app image did not start: {err.decode(errors='replace')[-200:]}"
    status, body = None, ""
    for _ in range(40):
        status, body = await sb.wget("/health", port=a["port"])
        if status:
            break
        await asyncio.sleep(0.5)
    _timed(cell, f"app boot {when}", t)
    try:
        health = json.loads(body)
    except json.JSONDecodeError:
        health = {}
    ok = status == 200 and health.get("ok") is True and health.get("schema_version") == a["schema"]
    if not ok and not health.get("error"):
        # busybox wget drops the body of a 5xx, so the app's own log says why it refused.
        _, out, err = await docker("logs", "--tail", "5", sb.app_name)
        health["error"] = (err or out).decode(errors="replace").strip()[-300:]
    cell.check(f"app /health ({when})", ok, f"HTTP {status}: " + (body[:200] or health.get("error", "")))
    if not ok:
        return False, health.get("error") or "app gave no answer"
    status, body = await sb.wget("/api/dashboard", port=a["port"])
    try:
        counts = json.loads(body).get("counts", {})
    except json.JSONDecodeError:
        counts = {}
    want = {k: v for k, v in cell.backup["manifest"]["row_counts"].items() if k in counts}
    dash_ok = status == 200 and counts == want
    cell.check(f"app /api/dashboard counts = manifest ({when})", dash_ok, f"HTTP {status}" + ("" if dash_ok else f" {body[:200]}"))
    pw = Path(ADMIN_PASSWORD_FILE).read_text().strip() if os.path.exists(ADMIN_PASSWORD_FILE) else ""
    good, _ = await sb.wget("/api/login", json.dumps({"email": ADMIN_EMAIL, "password": pw}), port=a["port"])
    wrong, _ = await sb.wget("/api/login", json.dumps({"email": ADMIN_EMAIL, "password": pw + "x"}), port=a["port"])
    login_ok = good == 200 and wrong == 401
    cell.check(f"seeded admin login ({when})", login_ok, f"right password HTTP {good}, wrong password HTTP {wrong}")
    if not (dash_ok and login_ok):
        return False, "app answers but its API disagrees with the backup"
    return True, ""


async def _app_schema(cell, sb):
    """The app's own migrations, read from its image (works on a stopped container too)."""
    rc, out, _ = await docker("cp", f"{sb.app_name}:{cell.app['migrations']}", "-")
    if rc != 0:
        return ""
    with tarfile.open(fileobj=io.BytesIO(out)) as tar:
        return "\n".join(f"-- {m.name}\n" + tar.extractfile(m).read().decode()
                         for m in sorted(tar.getmembers(), key=lambda m: m.name) if m.isfile() and m.name.endswith(".sql"))


async def _boot_and_bridge(cell, sb):
    ok, error = await _app_probe(cell, sb, "native")
    if ok:
        cell.state = "native"
        cell.reason = (f"restored and app {cell.app['version']} runs on it" +
                       (f" (app migrated schema v{cell.backup['manifest']['schema_version']} to v{cell.app['schema']} itself)"
                        if cell.backup["manifest"]["schema_version"] != cell.app["schema"] else ""))
        await _contract(cell, sb, "after app start", skip_counts=("schema_version",))
        return
    if cell.backup["manifest"]["schema_version"] == cell.app["schema"]:
        cell.state, cell.reason = "failed", f"app {cell.app['version']} does not run on the restored data: {error}"
        return
    target = await _app_schema(cell, sb)
    _, current, _ = await sb.exec("pg_dump", "-s", "--no-owner", "-h", "/var/run/postgresql", "-U", "postgres", "restored")
    current = "\n".join(l for l in current.splitlines() if l and not l.startswith(("--", "SET ", "SELECT pg_catalog")))
    _, sample, _ = await sb.psql("SELECT json_agg(c) FROM (SELECT * FROM customers ORDER BY id LIMIT 2) c")
    prompt = (f"Backup schema (v{cell.backup['manifest']['schema_version']}, restored now):\n{current[:12000]}\n\n"
              f"App {cell.app['version']} expects schema v{cell.app['schema']}; its migrations, in order:\n{target[:8000]}\n\n"
              f"Sample customers rows: {sample[:1000]}\n\nApp error: {error}")
    reply = await airlock.llm(cell, "bridge", BRIDGE_SYSTEM, prompt)
    for rnd in range(1, MAX_BRIDGE_ROUNDS + 1):
        sql = airlock._script(reply)
        name = f"{cell.backup['id']}-to-{cell.app['version']}-r{rnd}.sql"
        bridge = {"name": name, "round": rnd, "sha256": airlock._sha(sql), "sql": sql,
                  "changes": airlock._section(reply, "CHANGES") or airlock._section(reply, "CAUSE")}
        cell.bridges.append(bridge)
        cell.emit({"type": "bridge", "round": rnd, "name": name, "changes": bridge["changes"]})
        if re.search(r"\bPROGRAM\b|\bCOPY\b", sql, re.I):
            failure = "bridge uses COPY/PROGRAM, refused by policy"
        else:
            rc, _, err = await sb.psql(sql, timeout=60)
            failure = "" if rc == 0 else f"bridge SQL failed: {airlock._last_line(err)}"
        if not failure:
            ok, error = await _app_probe(cell, sb, f"bridge round {rnd}")
            failure = "" if ok else f"app still refuses: {error}"
        if not failure:
            # A bridge may legitimately change schema_version's history; every data table must survive intact.
            if await _contract(cell, sb, f"bridge round {rnd}", skip_counts=("schema_version",)):
                cell.state = "healed"
                cell.reason = (f"schema v{cell.backup['manifest']['schema_version']} backup bridged to app "
                               f"{cell.app['version']} (v{cell.app['schema']}) by GLM in round {rnd}")
                return
            failure = cell.reason
        bridge["failure"] = failure
        if rnd == MAX_BRIDGE_ROUNDS:
            break
        # A failed round may have committed nothing (one transaction) or left a half-bridged db: start clean.
        await sb.exec("dropdb", "-h", "/var/run/postgresql", "-U", "postgres", "--force", "restored")
        await sb.exec("createdb", "-h", "/var/run/postgresql", "-U", "postgres", "restored")
        await sb.exec("pg_restore", "--exit-on-error", "--no-owner", "-h", "/var/run/postgresql", "-U", "postgres",
                      "-d", "restored", "/input/backup.dump", timeout=150)
        reply = await airlock.llm(cell, "bridge repair", BRIDGE_REPAIR_SYSTEM,
                                  f"{prompt}\n\nPrevious SQL:\n{sql}\n\nFailure: {failure}")
    cell.state = "failed"
    cell.reason = f"no working bridge to app {cell.app['version']} after {MAX_BRIDGE_ROUNDS} rounds: {cell.bridges[-1].get('failure')}"


def certificate(backup, cells, sandbox_policy=None):
    """BackupAirlock's DR Readiness Certificate, one backup's matrix row, sealed by its own sha256."""
    states = {c["state"] for c in cells}
    cert = {
        "kind": "BackupAirlock DR Readiness Certificate", "issuer": "RestoreGuard",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "backup_sha256": {backup["file"]: backup["manifest"]["sha256"]},
        "sandbox_policy": sandbox_policy or policy(), "inference": "Vultr Serverless Inference (api.vultrinference.com/v1)",
        "matrix": [{
            "backup": backup["file"], "sha256": backup["manifest"]["sha256"], "bytes": backup["bytes"],
            "schema_version": backup["manifest"]["schema_version"],
            "status": ("QUARANTINED: DO NOT RESTORE TO PROD" if "quarantined" in states else
                       "DR READY" if states & {"native", "healed"} else "NOT RESTORABLE"),
            "app_versions": [{"app": c["app_version"], "result": c["state"], "detail": c["reason"],
                              "restored_into": c["restored_into"]["instance"],
                              "bridge": c["bridges"][-1]["name"] if c["state"] == "healed" else None,
                              "bridge_sha256": c["bridges"][-1]["sha256"] if c["state"] == "healed" else None,
                              "control_plane_checks": [{k: x[k] for k in ("check", "pass", "detail")} for x in c["checks"]]}
                             for c in cells]}],
        "llm_calls": [x for c in cells for x in c["llm_calls"]],
    }
    cert["proof_sha256"] = airlock._sha(json.dumps(cert, sort_keys=True))
    return cert
