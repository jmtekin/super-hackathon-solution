"""Restorer control plane: turns a backup into a verified restore report.

One run = plan (LLM) -> sandbox attempt -> on failure feed stderr back for a
repair (LLM) -> sandbox again -> deterministic verification -> explanation (LLM)
-> Recovery Proof. A backup chain (fixture "drift") runs a second pipeline:
every night is restored into the current app in parallel sandboxes, the model
writes one migration for the nights whose schema is older, and each migrated
night is checked against a manifest made by a reference migration the model
never sees. The model only writes scripts and prose; the verdict comes
from the sandbox outcome and verify.py. The Vultr Serverless Inference key lives
only in this process and is never written into a sandbox.

Every page and API path is relative so the app works behind Tailscale Funnel's
/restorer prefix (Funnel strips it) and at / on the Vultr VM alike.
"""
import asyncio
import difflib
import hashlib
import html
import json
import os
import re
import time
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

import sandbox
import verify

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
API = "https://api.vultrinference.com/v1/chat/completions"
PLAN_MODEL, FAST_MODEL = "glm-5.3", "deepseek-v4.1-flash-1017"
MAX_ATTEMPTS = 3

FIXTURES = {
    "recoverable": {"title": "Recoverable, first plan fails",
                    "goal": "Restore the shop-orders database and prove that orders and users are intact.",
                    "blurb": "Nightly SQLite dump of the shop app. The catalog is out of date.", "timeout": 60},
    "corrupted": {"title": "Corrupted backup",
                  "goal": "Restore the shop-orders database and prove that orders and users are intact.",
                  "blurb": "Restores without a single error. Is the data all there?", "timeout": 60},
    "compromised": {"title": "Compromised backup",
                    "goal": "Restore the shop-orders database, run its post-restore hooks, and prove the data is intact.",
                    "blurb": "Ships a post-restore hook. Nobody has read it.", "timeout": 3},
    "drift": {"title": "Backup chain with schema drift",
              "goal": "Prove that every nightly backup still restores into today's shop app.",
              "blurb": "10 nightly backups. The app's schema changed after night 9.", "timeout": 60, "kind": "chain"},
}

PLAN_SYSTEM = """You are Restorer's restore planner. Write a POSIX sh script (busybox ash; the sqlite3 CLI, gunzip, tar are available; there is no network) that restores a backup inside a sandbox.
- The backup is mounted read-only at /input/backup. Paths in CATALOG.json are relative to /input.
- Write the restored SQLite database to /work/restored.db. /work is the only writable directory.
- Follow the catalog exactly, including every post_restore_hooks entry (run each with sh after the database is restored).
- Keep it minimal: start with set -eu, no existence checks, no fallbacks, no guessing alternative paths. Echo one short line before each step.
Reply with exactly two parts: a line starting PLAN: followed by 2 to 4 short bullet lines, then the script in one ```sh block."""

REPAIR_SYSTEM = """You are Restorer's restore repairer. A restore script failed inside the sandbox. Fix it with the smallest change that addresses the error, using only facts from the error output and the file listing. Same rules as before: POSIX sh, set -eu, output to /work/restored.db, no network.
Reply with a line starting CAUSE: (one sentence), then the full corrected script in one ```sh block."""

MIGRATE_SYSTEM = """You are Restorer's migration writer. Old backups no longer load into the current app because its schema changed. Write one SQLite migration that copies every row from the old schema into the current one.
- It runs with the sqlite3 CLI on the restored database, which already has the current schema and is empty. The old backup, loaded into its own schema, is attached as old (refer to old.<table>).
- Plain SQL only: INSERT INTO ... SELECT with explicit column lists. No CREATE, DROP, ALTER, PRAGMA or ATTACH.
- Derive new columns from the old data where the data allows; otherwise let the column default apply.
Reply with a line starting CHANGES: followed by one short line per schema change, then the SQL in one ```sql block."""

REPAIR_MIGRATION_SYSTEM = """You are Restorer's migration repairer. A schema migration failed inside the sandbox. Fix it with the smallest change that addresses the error. Same rules: plain SQL, INSERT INTO ... SELECT with explicit column lists, the old database attached as old.
Reply with a line starting CAUSE: (one sentence), then the full corrected SQL in one ```sql block."""

# The chain's restore scripts are the app's own procedure, not the model's: only
# the migration is generated, so a failure points at the backup or the migration.
CHAIN_RESTORE = """set -eu
echo "restore: current app schema"
sqlite3 -bail /work/restored.db < /job/app_schema.sql
echo "restore: load backup data"
sqlite3 -bail /work/restored.db < /input/backup/data.sql
"""
CHAIN_ADAPT = """set -eu
echo "stage: backup into its own schema"
sqlite3 -bail /work/old.db < /input/backup/schema.sql
sqlite3 -bail /work/old.db < /input/backup/data.sql
echo "stage: current app schema"
sqlite3 -bail /work/restored.db < /job/app_schema.sql
echo "migrate: generated migration"
{ echo "ATTACH '/work/old.db' AS old;"; cat /job/migrate.sql; } | sqlite3 -bail /work/restored.db
"""

REPORT_SYSTEM = """You are Restorer's report writer. Explain the evidence of a backup restore drill to an on-call engineer in 3 short sentences: what happened, what the deterministic checks or sandbox enforcement showed, and what to do next. The verdict is already decided; do not contradict it. Plain words, no markdown, no em-dashes."""

app = FastAPI(title="Restorer", docs_url=None, redoc_url=None)
runs = {}


def _key():
    if k := os.environ.get("VULTR_INFERENCE_KEY"):
        return k.strip()
    return Path(os.environ.get("VULTR_INFERENCE_KEY_FILE", "/opt/agent/.config/vultr-inference-key")).read_text().strip()


def _chat(model, system, user):
    # Low reasoning effort: GLM-5.3 at default effort spends ~20 s thinking about a
    # five-line script, which is dead air in a live demo (measured 16-25 s vs ~1-2 s).
    body = {"model": model, "max_tokens": 4000, "temperature": 0.2, "reasoning_effort": "low",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        d = json.load(r)
    text = d["choices"][0]["message"].get("content") or ""
    if not text.strip():
        raise ValueError("empty completion")
    return text, d.get("usage", {})


async def llm(run, purpose, system, user):
    """GLM-5.3 first; DeepSeek Flash if it errors or stalls, so a slow model never stalls the demo."""
    for model in (PLAN_MODEL, FAST_MODEL):
        t = time.monotonic()
        run.emit({"type": "llm", "phase": "start", "purpose": purpose, "model": model})
        try:
            text, usage = await asyncio.to_thread(_chat, model, system, user)
        except Exception as e:
            run.emit({"type": "llm", "phase": "error", "purpose": purpose, "model": model, "error": str(e)[:200]})
            continue
        call = {"purpose": purpose, "model": model, "latency_s": round(time.monotonic() - t, 2),
                "tokens": usage.get("total_tokens"), "fallback": model != PLAN_MODEL}
        run.llm_calls.append(call)
        run.emit({"type": "llm", "phase": "done", **call})
        return text
    raise RuntimeError(f"{purpose}: both models failed")


def _script(text):
    m = re.search(r"```(?:[\w-]+)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def _section(text, tag):
    m = re.search(rf"{tag}:\s*(.*?)(?:```|$)", text, re.S)
    return m.group(1).strip() if m else ""


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def _schema_version(sql):
    m = re.search(r"PRAGMA user_version\s*=\s*(\d+)", sql)
    return int(m.group(1)) if m else 0


def _last_line(text):
    return (text.strip().splitlines() or [""])[-1][:300]


def _diff(a, b, name_a, name_b):
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), name_a, name_b))


class Run:
    def __init__(self, fixture):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.fixture, self.cfg = fixture, FIXTURES[fixture]
        self.dir = RUNS / self.id
        self.t0, self.events, self.llm_calls, self.attempts = time.monotonic(), [], [], []
        self.cond, self.done, self.proof = asyncio.Condition(), False, None

    def emit(self, ev):
        ev["t"] = round(time.monotonic() - self.t0, 2)
        self.events.append(ev)
        asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self):
        async with self.cond:
            self.cond.notify_all()

    def step(self, name, status, **kw):
        self.emit({"type": "step", "name": name, "status": status, **kw})


async def drill(run):
    fx = HERE / "fixtures" / run.fixture
    backup, expected = fx / "backup", json.loads((fx / "expected.json").read_text())
    files = {str(p.relative_to(fx)): _sha(p.read_bytes()) for p in sorted(backup.rglob("*")) if p.is_file()}
    listing = "\n".join(f"/input/{p.relative_to(fx)}  {p.stat().st_size} bytes"
                        for p in sorted(backup.rglob("*")) if p.is_file())
    catalog = (backup / "CATALOG.json").read_text()
    run.step("Backup received", "ok", detail=f"{len(files)} files fingerprinted (sha256), mounted read-only",
             evidence=files)
    verdict, checks, artifact_sha, explanation = None, [], None, ""
    try:
        run.step("Plan restore", "running", detail="Vultr Serverless Inference writes plan + script")
        text = await llm(run, "plan", PLAN_SYSTEM, f"Goal: {run.cfg['goal']}\n\nCATALOG.json:\n{catalog}")
        script = _script(text)
        run.step("Plan restore", "ok", plan=_section(text, "PLAN"), script=script)
        result = None
        for n in range(1, MAX_ATTEMPTS + 1):
            run.step(f"Sandbox attempt {n}", "running", detail="fresh container, network none")
            result = await sandbox.run(backup, script, run.dir / f"attempt-{n}", run.cfg["timeout"], run.emit)
            run.attempts.append({"n": n, "script": script, "script_sha256": _sha(script),
                                 **{k: result[k] for k in ("name", "exit", "elapsed", "stdout", "stderr",
                                                           "network_denied", "timed_out", "destroyed")}})
            ok = result["exit"] == 0 and result["artifact"] is not None
            run.step(f"Sandbox attempt {n}", "ok" if ok else "fail", exit=result["exit"], elapsed=result["elapsed"],
                     stderr=result["stderr"][-3000:], destroyed=result["destroyed"])
            if ok or result["timed_out"] or result["network_denied"] or n == MAX_ATTEMPTS:
                break
            run.step("Repair script", "running", detail="stderr goes back to the model")
            text = await llm(run, "repair", REPAIR_SYSTEM,
                             f"Goal: {run.cfg['goal']}\n\nCATALOG.json:\n{catalog}\nFiles in the input volume:\n{listing}\n\n"
                             f"Script:\n```sh\n{script}```\nExit code: {result['exit']}\n"
                             f"stderr:\n{result['stderr'][-3000:]}\nstdout:\n{result['stdout'][-1500:]}")
            fixed = _script(text)
            diff = "".join(difflib.unified_diff(script.splitlines(True), fixed.splitlines(True),
                                                f"restore.sh (attempt {n})", f"restore.sh (attempt {n + 1})"))
            run.attempts[-1]["repair"] = {"cause": _section(text, "CAUSE"), "diff": diff}
            run.step("Repair script", "ok", cause=_section(text, "CAUSE"), diff=diff)
            script = fixed

        if result["timed_out"] or result["network_denied"]:
            verdict = {"level": "unsafe", "headline": "Backup unsafe; production untouched.",
                       "reason": ("outbound network attempt denied; " if result["network_denied"] else "")
                       + ("killed at the wall-clock limit" if result["timed_out"] else "")}
            run.step("Verification", "skipped", detail="sandbox was terminated; nothing from it is trusted")
        elif result["artifact"] is None:
            verdict = {"level": "failed", "headline": "Restore failed after repairs.",
                       "reason": f"{len(run.attempts)} attempts, last exit {result['exit']}"}
        else:
            db = run.dir / "restored.db"
            db.write_bytes(result["artifact"][1])
            artifact_sha = _sha(result["artifact"][1])
            run.step("Verification", "running", detail="deterministic checks, no model involved")
            checks = verify.run(db, expected)
            for c in checks:
                run.emit({"type": "check", **c})
            good = all(c["pass"] for c in checks)
            run.step("Verification", "ok" if good else "fail",
                     detail=f"{sum(c['pass'] for c in checks)}/{len(checks)} checks passed")
            verdict = ({"level": "ok", "headline": "Backup verified: restore works and data is intact.",
                        "reason": f"{len(checks)}/{len(checks)} deterministic checks passed"} if good else
                       {"level": "corrupt", "headline": "Backup corrupted: it restores, but the data is not what was backed up.",
                        "reason": "; ".join(c["detail"] for c in checks if not c["pass"])})
        explanation = await explain(run, {
            "fixture": run.cfg["title"], "verdict": verdict,
            "attempts": [{k: a.get(k) for k in ("n", "exit", "elapsed", "network_denied", "timed_out")}
                         | {"stderr_tail": a["stderr"][-400:]} for a in run.attempts],
            "checks": [{k: c[k] for k in ("check", "pass", "detail")} for c in checks]})
    except Exception as e:
        verdict = verdict or {"level": "failed", "headline": "Drill aborted.", "reason": str(e)[:300]}
        run.step("Error", "fail", detail=str(e)[:300])
    await seal(run, verdict, explanation, files, restored_db_sha256=artifact_sha, checks=checks)


async def explain(run, summary):
    run.step("Explain evidence", "running")
    text = (await llm(run, "report", REPORT_SYSTEM, json.dumps(summary, indent=1))).strip()
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")
    run.step("Explain evidence", "ok", text=text)
    return text


async def seal(run, verdict, explanation, files, **extra):
    """Write the Recovery Proof and end the run's event stream."""
    run.proof = {
        "kind": "Restorer Recovery Proof", "run_id": run.id, "fixture": run.fixture, "goal": run.cfg["goal"],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "elapsed_s": round(time.monotonic() - run.t0, 2),
        "verdict": verdict, "backup_sha256": files, "sandbox_policy": sandbox.policy(run.cfg["timeout"]),
        "command_receipts": [{k: v for k, v in a.items() if k != "stdout"} for a in run.attempts],
        "llm_calls": run.llm_calls, "explanation": explanation,
        "inference": "Vultr Serverless Inference (api.vultrinference.com/v1)", **extra,
    }
    run.proof["proof_sha256"] = _sha(json.dumps(run.proof, sort_keys=True))
    run.dir.mkdir(parents=True, exist_ok=True)
    (run.dir / "proof.json").write_text(json.dumps(run.proof, indent=2))
    run.emit({"type": "verdict", **verdict, "elapsed_s": run.proof["elapsed_s"], "proof_sha256": run.proof["proof_sha256"]})
    run.done = True
    await run._notify()


async def chain_drill(run):
    root = HERE / "fixtures" / run.fixture
    app_schema = (root / "app" / "schema.sql").read_text()
    app_v = _schema_version(app_schema)
    chain = []
    for d in sorted(root.glob("nightly-*")):
        b = d / "backup"
        chain.append({"backup": d.name, "dir": b, "expected": json.loads((d / "expected.json").read_text()),
                      "catalog": json.loads((b / "CATALOG.json").read_text()), "schema": (b / "schema.sql").read_text()})
    files = {f"{c['backup']}/{p.name}": _sha(p.read_bytes()) for c in chain for p in sorted(c["dir"].iterdir())}
    run.emit({"type": "chain", "phase": "init", "app_version": app_v,
              "backups": [{"backup": c["backup"], "taken_at": c["catalog"]["taken_at"],
                           "schema_version": c["catalog"]["schema_version"]} for c in chain]})
    run.step("Backup chain received", "ok", detail=f"{len(chain)} nightly backups, {len(files)} files fingerprinted "
             "(sha256), each mounted read-only into its own sandbox")

    def tagged(c):
        def emit(ev):
            # Ten parallel CPU traces would only be noise; the per-night outcome is what the chain shows.
            if ev["type"] != "cpu":
                run.emit({**ev, "backup": c["backup"]})
        return emit

    async def wave(label, todo, script, job):
        results = await asyncio.gather(*(
            sandbox.run(c["dir"], script, run.dir / f"{c['backup']}-{label}", run.cfg["timeout"], tagged(c),
                        {"app_schema.sql": app_schema, **job}) for c in todo))
        for c, r in zip(todo, results):
            r["ok"] = r["exit"] == 0 and r["artifact"] is not None
            c["result"] = r
            run.attempts.append({"n": f"{c['backup']} {label}", "script": script, "script_sha256": _sha(script),
                                 **{f"{f.split('.')[0]}_sha256": _sha(t) for f, t in job.items()},
                                 **{k: r[k] for k in ("name", "exit", "elapsed", "stdout", "stderr",
                                                      "network_denied", "timed_out", "destroyed")}})
            run.emit({"type": "chain", "phase": label.split("-")[0], "backup": c["backup"], "ok": r["ok"],
                      "exit": r["exit"], "elapsed": r["elapsed"], "destroyed": r["destroyed"],
                      "error": "timeout" if r["timed_out"] else _last_line(r["stderr"])})
        return [(c, c["result"]) for c in todo if not c["result"]["ok"]]

    verdict, explanation, migration, old_v, rounds = None, "", None, None, 0
    try:
        run.step("Native restore", "running", detail=f"{len(chain)} sandboxes in parallel, current app schema v{app_v}")
        broken = [c for c, _ in await wave("native", chain, CHAIN_RESTORE, {})]
        run.step("Native restore", "fail" if broken else "ok",
                 detail=f"{len(chain) - len(broken)} of {len(chain)} load into the current app"
                 + (f"; {len(broken)} incompatible: {_last_line(broken[-1]['result']['stderr'])[:160]}"
                    if broken else ""))
        if broken:
            if len({c["schema"] for c in broken}) > 1:
                raise RuntimeError("the incompatible backups span more than one old schema; "
                                   "one migration per old schema is not built yet")
            newest = broken[-1]
            old_schema, old_v = newest["schema"], newest["catalog"]["schema_version"]
            schema_diff = _diff(old_schema, app_schema, f"schema v{old_v} (backup)", f"schema v{app_v} (current app)")
            data = (newest["dir"] / "data.sql").read_text().splitlines()
            sample = "\n".join(next(l for l in data if l.startswith(f'INSERT INTO "{t}"'))
                               for t in re.findall(r"CREATE TABLE (\w+)", old_schema))
            context = (f"Old schema (v{old_v}, shipped with the backup):\n```sql\n{old_schema}```\n"
                       f"Current app schema (v{app_v}):\n```sql\n{app_schema}```\n"
                       f"One sample row per old table:\n{sample}\n")
            run.step("Write migration", "running", detail=f"Vultr Serverless Inference reads schema v{old_v} against v{app_v}")
            text = await llm(run, "migration", MIGRATE_SYSTEM,
                             context + f"\nNative restore of {newest['backup']} into the current app failed:\n"
                             f"{newest['result']['stderr'][-1500:]}")
            migration = _script(text)
            # Models repeat the CHANGES: tag or add bullets on every line; the UI wants bare lines.
            changes = re.sub(r"(?m)^\s*(?:CHANGES:|[-*])\s*", "", _section(text, "CHANGES"))
            run.step("Write migration", "ok", changes=changes, migration=migration,
                     schema_diff=schema_diff, from_version=old_v, to_version=app_v)
            for rounds in range(1, MAX_ATTEMPTS + 1):
                name = f"Restore + migrate, round {rounds}"
                run.step(name, "running", detail=f"{len(broken)} sandboxes in parallel: load into schema v{old_v}, "
                         f"migrate to v{app_v}")
                failed = await wave(f"adapt-{rounds}", broken, CHAIN_ADAPT, {"migrate.sql": migration})
                run.step(name, "fail" if failed else "ok", detail=f"{len(broken) - len(failed)} of {len(broken)} adapted")
                if not failed or rounds == MAX_ATTEMPTS:
                    break
                c, r = failed[0]
                run.step("Repair migration", "running", detail="stderr goes back to the model")
                text = await llm(run, "repair", REPAIR_MIGRATION_SYSTEM,
                                 context + f"\nMigration:\n```sql\n{migration}```\nIt failed on {c['backup']} with exit "
                                 f"{r['exit']}.\nstderr:\n{r['stderr'][-3000:]}")
                fixed = _script(text)
                diff = _diff(migration, fixed, f"migrate.sql (round {rounds})", f"migrate.sql (round {rounds + 1})")
                run.step("Repair migration", "ok", cause=_section(text, "CAUSE"), diff=diff)
                migration = fixed

        run.step("Verification", "running", detail="each night against its own manifest, no model involved")
        for c in chain:
            c["adapted"], c["checks"], c["db_sha"] = c in broken, [], None
            if not c["result"]["ok"]:
                continue
            db = run.dir / f"{c['backup']}.db"
            db.write_bytes(c["result"]["artifact"][1])
            c["db_sha"] = _sha(c["result"]["artifact"][1])
            c["checks"] = verify.run(db, c["expected"])
            run.emit({"type": "chain", "phase": "verify", "backup": c["backup"],
                      "checks": [{f: k[f] for f in ("check", "pass", "detail")} for k in c["checks"]]})
        checks = [k for c in chain for k in c["checks"]]
        good = [c for c in chain if c["checks"] and all(k["pass"] for k in c["checks"])]
        n_pass = sum(k["pass"] for k in checks)
        run.step("Verification", "ok" if len(good) == len(chain) else "fail",
                 detail=f"{len(good)}/{len(chain)} backups pass every check ({n_pass}/{len(checks)} checks)")
        migrated = sum(c["adapted"] for c in good)
        if len(good) == len(chain):
            verdict = {"level": "ok", "headline": f"Backup chain verified: all {len(chain)} backups restore into the current app.",
                       "reason": (f"{migrated} migrated from schema v{old_v} to v{app_v}, " if migrated else "")
                       + f"{len(chain) - migrated} native; {n_pass}/{len(checks)} deterministic checks passed"}
        else:
            bad = [c for c in chain if c not in good]
            verdict = {"level": "failed" if any(not c["result"]["ok"] for c in bad) else "corrupt",
                       "headline": f"Backup chain broken: {len(bad)} of {len(chain)} backups do not restore into the current app.",
                       "reason": "; ".join(f"{c['backup']}: " + ("restore failed" if not c["result"]["ok"] else
                                           ", ".join(k["check"] for k in c["checks"] if not k["pass"])) for c in bad)}
        explanation = await explain(run, {
            "fixture": run.cfg["title"], "verdict": verdict, "app_schema_version": app_v,
            "migration": {"from": old_v, "to": app_v, "rounds": rounds} if migration else None,
            "chain": [{"backup": c["backup"], "native_restore_error": None if not c["adapted"] else
                       c["result"]["stderr"][-200:] if not c["result"]["ok"] else "incompatible, migrated",
                       "checks_failed": [k["check"] for k in c["checks"] if not k["pass"]]} for c in chain]})
    except Exception as e:
        verdict = verdict or {"level": "failed", "headline": "Drill aborted.", "reason": str(e)[:300]}
        run.step("Error", "fail", detail=str(e)[:300])
    note = {True: f"migrated from schema v{old_v} to v{app_v}", False: f"native schema v{app_v}"}
    await seal(run, verdict, explanation, files, checks=[{**k, "check": f"{c['backup']} {k['check']}"}
                                                         for c in chain for k in c.get("checks", [])],
               restored_db_sha256=None,
               chain=[{"backup": c["backup"], "taken_at": c["catalog"]["taken_at"],
                       "schema_version": c["catalog"]["schema_version"],
                       "note": note[c.get("adapted", False)] if c.get("checks") else "not restored",
                       "restored_db_sha256": c.get("db_sha"),
                       "checks_passed": f"{sum(k['pass'] for k in c.get('checks', []))}/{len(c.get('checks', []))}"}
                      for c in chain],
               migration={"from_version": old_v, "to_version": app_v, "sql": migration, "sha256": _sha(migration),
                          "rounds": rounds} if migration else None)


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "service": "restorer-control-plane", "runs_active": sum(not r.done for r in runs.values())}


@app.get("/api/fixtures")
def fixtures():
    return [{"id": k, "kind": v.get("kind", "single"), **{f: v[f] for f in ("title", "goal", "blurb", "timeout")}}
            for k, v in FIXTURES.items()]


@app.post("/api/runs/{fixture}")
async def start(fixture: str):
    if fixture not in FIXTURES:
        raise HTTPException(404, "unknown fixture")
    # One drill at a time: the box is shared, and a judge double-clicking must not fork two runs.
    if any(not r.done for r in runs.values()):
        raise HTTPException(409, "a drill is already running")
    run = Run(fixture)
    runs[run.id] = run
    asyncio.get_running_loop().create_task(chain_drill(run) if run.cfg.get("kind") == "chain" else drill(run))
    return {"run_id": run.id}


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str):
    if (run := runs.get(run_id)) is None:
        raise HTTPException(404, "unknown run")

    async def stream():
        i = 0
        while True:
            while i < len(run.events):
                yield f"data: {json.dumps(run.events[i])}\n\n"
                i += 1
            if run.done and i >= len(run.events):
                yield "event: end\ndata: {}\n\n"
                return
            async with run.cond:
                try:
                    await asyncio.wait_for(run.cond.wait(), 10)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _proof(run_id):
    p = RUNS / Path(run_id).name / "proof.json"
    if not p.is_file():
        raise HTTPException(404, "no proof for this run")
    return json.loads(p.read_text())


@app.get("/api/runs/{run_id}/proof.json")
def proof_json(run_id: str):
    return JSONResponse(_proof(run_id), headers={"Content-Disposition": f'attachment; filename="recovery-proof-{run_id}.json"'})


@app.get("/api/runs/{run_id}/proof.html", response_class=HTMLResponse)
def proof_html(run_id: str):
    p, e = _proof(run_id), html.escape
    v = p["verdict"]
    rows = "".join(f"<tr><td>{'PASS' if c['pass'] else 'FAIL'}</td><td>{e(c['check'])}</td><td>{e(c['detail'])}</td></tr>"
                   for c in p["checks"]) or "<tr><td colspan=3>Not run: sandbox terminated</td></tr>"
    receipts = "".join(f"<h3>{'Attempt ' if isinstance(a['n'], int) else ''}{e(str(a['n']))}: exit {e(str(a['exit']))}, {a['elapsed']} s, "
                       f"{'destroyed' if a['destroyed'] else 'NOT destroyed'}</h3><code>sha256 {a['script_sha256']}</code>"
                       f"<pre>{e(a['script'])}</pre>" + (f"<pre class=err>{e(a['stderr'])}</pre>" if a['stderr'] else "")
                       for a in p["command_receipts"])
    if p.get("chain"):
        m = p.get("migration")
        restored = ("<table>" + "".join(f"<tr><td>{e(c['backup'])}</td><td>{e(c['taken_at'])}</td><td>schema v{c['schema_version']}</td>"
                                        f"<td>{e(c['note'])}</td><td>{e(c['checks_passed'])}</td>"
                                        f"<td><code>{(c['restored_db_sha256'] or 'none')[:16]}</code></td></tr>" for c in p["chain"])
                    + "</table>" + (f"<h3>Migration v{m['from_version']} to v{m['to_version']}, {m['rounds']} round(s)</h3>"
                                    f"<code>sha256 {m['sha256']}</code><pre>{e(m['sql'])}</pre>" if m else ""))
        restored_title = "Backup chain"
    else:
        restored, restored_title = f"<code>sha256 {p['restored_db_sha256'] or 'none'}</code>", "Restored database"
    body = f"""<!doctype html><meta charset=utf-8><title>Recovery Proof {e(p['run_id'])}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;color:#0f172a}}
pre{{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;overflow:auto;font-size:12px}}pre.err{{color:#fca5a5}}
table{{border-collapse:collapse;width:100%}}td{{border-bottom:1px solid #e2e8f0;padding:6px}}code{{font-size:12px;color:#475569}}
.v{{padding:16px;border-radius:10px;font-weight:600;background:{'#dcfce7' if v['level'] == 'ok' else '#fee2e2'}}}</style>
<h1>Recovery Proof</h1><p>Run {e(p['run_id'])} &middot; {e(p['fixture'])} &middot; {e(p['generated_at'])} &middot; {p['elapsed_s']} s</p>
<div class=v>{e(v['headline'])}<br><small>{e(v['reason'])}</small></div><p>{e(p['explanation'])}</p>
<h2>{restored_title}</h2>{restored}<h2>Checks</h2><table>{rows}</table>
<h2>Command receipts</h2>{receipts}<h2>Sandbox policy</h2><pre>{e(json.dumps(p['sandbox_policy'], indent=1))}</pre>
<h2>Backup fingerprints</h2><pre>{e(json.dumps(p['backup_sha256'], indent=1))}</pre>
<p><code>proof sha256 {p['proof_sha256']}</code></p>"""
    return HTMLResponse(body, headers={"Content-Disposition": f'attachment; filename="recovery-proof-{run_id}.html"'})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("RESTORER_HOST", "127.0.0.1"), port=int(os.environ.get("RESTORER_PORT", "8530")))
