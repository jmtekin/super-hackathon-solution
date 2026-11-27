"""Restorer control plane: turns a backup into a verified restore report.

One run = plan (LLM) -> sandbox attempt -> on failure feed stderr back for a
repair (LLM) -> sandbox again -> deterministic verification -> explanation (LLM)
-> Recovery Proof. The model only writes scripts and prose; the verdict comes
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
}

PLAN_SYSTEM = """You are Restorer's restore planner. Write a POSIX sh script (busybox ash; the sqlite3 CLI, gunzip, tar are available; there is no network) that restores a backup inside a sandbox.
- The backup is mounted read-only at /input/backup. Paths in CATALOG.json are relative to /input.
- Write the restored SQLite database to /work/restored.db. /work is the only writable directory.
- Follow the catalog exactly, including every post_restore_hooks entry (run each with sh after the database is restored).
- Keep it minimal: start with set -eu, no existence checks, no fallbacks, no guessing alternative paths. Echo one short line before each step.
Reply with exactly two parts: a line starting PLAN: followed by 2 to 4 short bullet lines, then the script in one ```sh block."""

REPAIR_SYSTEM = """You are Restorer's restore repairer. A restore script failed inside the sandbox. Fix it with the smallest change that addresses the error, using only facts from the error output and the file listing. Same rules as before: POSIX sh, set -eu, output to /work/restored.db, no network.
Reply with a line starting CAUSE: (one sentence), then the full corrected script in one ```sh block."""

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
    m = re.search(r"```(?:sh|bash|shell)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def _section(text, tag):
    m = re.search(rf"{tag}:\s*(.*?)(?:```|$)", text, re.S)
    return m.group(1).strip() if m else ""


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


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
        run.step("Explain evidence", "running")
        summary = {"fixture": run.cfg["title"], "verdict": verdict,
                   "attempts": [{k: a.get(k) for k in ("n", "exit", "elapsed", "network_denied", "timed_out")}
                                | {"stderr_tail": a["stderr"][-400:]} for a in run.attempts],
                   "checks": [{k: c[k] for k in ("check", "pass", "detail")} for c in checks]}
        explanation = (await llm(run, "report", REPORT_SYSTEM, json.dumps(summary, indent=1))).strip()
        explanation = explanation.replace(" \u2014 ", ", ").replace("\u2014", ", ")
        run.step("Explain evidence", "ok", text=explanation)
    except Exception as e:
        verdict = verdict or {"level": "failed", "headline": "Drill aborted.", "reason": str(e)[:300]}
        run.step("Error", "fail", detail=str(e)[:300])

    run.proof = {
        "kind": "Restorer Recovery Proof", "run_id": run.id, "fixture": run.fixture, "goal": run.cfg["goal"],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "elapsed_s": round(time.monotonic() - run.t0, 2),
        "verdict": verdict, "backup_sha256": files, "sandbox_policy": sandbox.policy(run.cfg["timeout"]),
        "command_receipts": [{k: a.get(k) for k in ("n", "name", "script_sha256", "script", "exit", "elapsed",
                                                     "stderr", "network_denied", "timed_out", "destroyed", "repair")}
                             for a in run.attempts],
        "restored_db_sha256": artifact_sha, "checks": checks, "llm_calls": run.llm_calls,
        "explanation": explanation, "inference": "Vultr Serverless Inference (api.vultrinference.com/v1)",
    }
    run.proof["proof_sha256"] = _sha(json.dumps(run.proof, sort_keys=True))
    run.dir.mkdir(parents=True, exist_ok=True)
    (run.dir / "proof.json").write_text(json.dumps(run.proof, indent=2))
    run.emit({"type": "verdict", **verdict, "elapsed_s": run.proof["elapsed_s"], "proof_sha256": run.proof["proof_sha256"]})
    run.done = True
    await run._notify()


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "service": "restorer-control-plane", "runs_active": sum(not r.done for r in runs.values())}


@app.get("/api/fixtures")
def fixtures():
    return [{"id": k, **{f: v[f] for f in ("title", "goal", "blurb", "timeout")}} for k, v in FIXTURES.items()]


@app.post("/api/runs/{fixture}")
async def start(fixture: str):
    if fixture not in FIXTURES:
        raise HTTPException(404, "unknown fixture")
    # One drill at a time: the box is shared, and a judge double-clicking must not fork two runs.
    if any(not r.done for r in runs.values()):
        raise HTTPException(409, "a drill is already running")
    run = Run(fixture)
    runs[run.id] = run
    asyncio.get_running_loop().create_task(drill(run))
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
    receipts = "".join(f"<h3>Attempt {a['n']}: exit {e(str(a['exit']))}, {a['elapsed']} s, "
                       f"{'destroyed' if a['destroyed'] else 'NOT destroyed'}</h3><code>sha256 {a['script_sha256']}</code>"
                       f"<pre>{e(a['script'])}</pre>" + (f"<pre class=err>{e(a['stderr'])}</pre>" if a['stderr'] else "")
                       for a in p["command_receipts"])
    body = f"""<!doctype html><meta charset=utf-8><title>Recovery Proof {e(p['run_id'])}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;color:#0f172a}}
pre{{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;overflow:auto;font-size:12px}}pre.err{{color:#fca5a5}}
table{{border-collapse:collapse;width:100%}}td{{border-bottom:1px solid #e2e8f0;padding:6px}}code{{font-size:12px;color:#475569}}
.v{{padding:16px;border-radius:10px;font-weight:600;background:{'#dcfce7' if v['level'] == 'ok' else '#fee2e2'}}}</style>
<h1>Recovery Proof</h1><p>Run {e(p['run_id'])} &middot; {e(p['fixture'])} &middot; {e(p['generated_at'])} &middot; {p['elapsed_s']} s</p>
<div class=v>{e(v['headline'])}<br><small>{e(v['reason'])}</small></div><p>{e(p['explanation'])}</p>
<h2>Checks</h2><table>{rows}</table><h2>Restored database</h2><code>sha256 {p['restored_db_sha256'] or 'none'}</code>
<h2>Command receipts</h2>{receipts}<h2>Sandbox policy</h2><pre>{e(json.dumps(p['sandbox_policy'], indent=1))}</pre>
<h2>Backup fingerprints</h2><pre>{e(json.dumps(p['backup_sha256'], indent=1))}</pre>
<p><code>proof sha256 {p['proof_sha256']}</code></p>"""
    return HTMLResponse(body, headers={"Content-Disposition": f'attachment; filename="recovery-proof-{run_id}.html"'})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("RESTORER_HOST", "127.0.0.1"), port=int(os.environ.get("RESTORER_PORT", "8530")))
