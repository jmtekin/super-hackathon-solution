"""RestoreGuard service: watches the ERP's backups and serves the results as JSON.

Jonas builds the UI, so this is API-first: every new backup in the watched
folder (or mirrored from the app's read-only /backups/ HTTP endpoint) is queued
and proved on every app version in apps.json by guard.run_cell, one sandbox at
a time; results persist under state/ so a restart loses nothing but the run in
flight, which is simply re-queued. A backup counts as new once its manifest
exists, because the backup job writes the manifest last.

Every path is relative so the service works behind a Funnel prefix and at /
on the Vultr VM alike. API: see README.md.
"""
import asyncio
import json
import os
import re
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

import frappe_target
import guard

HERE = Path(__file__).resolve().parent
DIRS = [Path(p) for p in os.environ.get("RESTOREGUARD_BACKUPS", str(HERE / "backups")).split(":")]
REMOTE = os.environ.get("RESTOREGUARD_BACKUPS_URL", "")
REMOTE_TOKEN_FILE = os.environ.get("RESTOREGUARD_BACKUPS_TOKEN_FILE", "/opt/agent/.config/restoreguard-backups-token")
POLL_S = int(os.environ.get("RESTOREGUARD_POLL", "15"))
RESULTS = guard.STATE / "results"
NAME = re.compile(r"^[\w.-]+_v\d+$")
FRAPPE = frappe_target.config()  # a test target that could be production stops the service here
FRAPPE_DIR = Path(os.environ.get("RESTOREGUARD_FRAPPE_BACKUPS", HERE / "backups-frappe"))
FRAPPE_POLL_S = int(os.environ.get("RESTOREGUARD_FRAPPE_POLL", "120"))  # production's Backups page, read only

backups, queue, subscribers = {}, None, set()


def broadcast(ev):
    for q in list(subscribers):
        if q.qsize() < 1000:
            q.put_nowait(ev)


def _save(b):
    RESULTS.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS / f".{b['id']}.json"
    tmp.write_text(json.dumps(b, indent=1))
    tmp.replace(RESULTS / f"{b['id']}.json")


def _status(b):
    b["updated_at"] = time.time()
    broadcast({"type": "backup", "id": b["id"], "status": b["status"], "reason": b.get("reason", "")})
    _save(b)


def _mirror():
    """Copy new backups from the app's read-only HTTP endpoint into the first watched folder."""
    token = Path(REMOTE_TOKEN_FILE).read_text().strip() if os.path.exists(REMOTE_TOKEN_FILE) else ""
    hdr = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(urllib.request.Request(REMOTE, headers=hdr), timeout=20) as r:
        listing = r.read().decode(errors="replace")
    for stem in sorted(set(re.findall(r"([\w.-]+_v\d+)\.json", listing))):
        if (DIRS[0] / f"{stem}.json").exists():
            continue
        for ext in (".dump", ".json"):  # manifest last, like the backup job
            req = urllib.request.Request(REMOTE.rstrip("/") + f"/{stem}{ext}", headers=hdr)
            tmp = DIRS[0] / f".{stem}{ext}.part"
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            tmp.replace(DIRS[0] / f"{stem}{ext}")


def discover():
    for bid in [k for k, b in backups.items() if b["status"] not in ("queued", "running") and not os.path.exists(b["path"])]:
        del backups[bid]  # its dump is gone (retention, a removed test copy): nothing left to prove or restore
        broadcast({"type": "backup", "id": bid, "status": "removed", "reason": "dump file gone"})
    for d in DIRS:
        for dump in sorted(d.glob("*_v*.dump")):
            if dump.stem in backups or not NAME.match(dump.stem) or not dump.with_suffix(".json").exists():
                continue
            try:
                rec = guard.load_backup(dump)
            except ValueError as e:
                rec = {"id": dump.stem, "file": dump.name, "path": str(dump), "manifest": None}
                backups[rec["id"]] = {**rec, "status": "failed", "reason": str(e), "found_at": time.time(), "cells": {}}
                _status(backups[rec["id"]])
                continue
            backups[rec["id"]] = {**rec, "status": "queued", "found_at": time.time(), "cells": {}}
            _status(backups[rec["id"]])
            queue.put_nowait(rec["id"])
    for gz in sorted(FRAPPE_DIR.glob("*.sql.gz")) if FRAPPE else []:
        stem = gz.name[:-len(".sql.gz")]
        if stem in backups or not frappe_target.FILE.match(gz.name) or not (FRAPPE_DIR / f"{stem}.json").exists():
            continue
        try:
            rec = frappe_target.load_backup(gz)
        except ValueError as e:
            backups[stem] = {"id": stem, "kind": "frappe", "file": gz.name, "path": str(gz), "manifest": None,
                             "status": "failed", "reason": str(e), "found_at": time.time(), "cells": {}}
            _status(backups[stem])
            continue
        backups[stem] = {**rec, "status": "queued", "found_at": time.time(), "cells": {}}
        _status(backups[stem])
        queue.put_nowait(stem)


async def watcher():
    frappe_due = 0
    while True:
        if FRAPPE and time.monotonic() >= frappe_due:
            frappe_due = time.monotonic() + FRAPPE_POLL_S
            try:
                await asyncio.to_thread(frappe_target.fetch_backups, FRAPPE, FRAPPE_DIR)
            except Exception as e:
                broadcast({"type": "watcher", "error": f"frappe fetch failed: {e}"[:300]})
        if REMOTE:
            try:
                await asyncio.to_thread(_mirror)
            except Exception as e:
                broadcast({"type": "watcher", "error": f"mirror failed: {e}"[:300]})
        discover()
        await asyncio.sleep(POLL_S)


async def worker():
    while True:
        bid = await queue.get()
        b = backups[bid]
        b.update(status="running", started_at=time.time(), cells={})
        _status(b)
        try:
            results = []
            runner = frappe_target if b.get("kind") == "frappe" else guard
            for a in _targets(b):
                r = await runner.run_cell(b, a, broadcast)
                results.append(r)
                events = r.pop("events")
                (RESULTS / f"{bid}@{a['version']}.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
                b["cells"][a["version"]] = r
                _save(b)
            b["certificate"] = guard.certificate(b, results, runner.policy())
            row = b["certificate"]["matrix"][0]["status"]
            b["status"] = {"DR READY": "dr_ready", "NOT RESTORABLE": "not_restorable"}.get(row, "quarantined")
            b["reason"] = "; ".join(f"{r['app_version']}: {r['state']}, {r['reason']}" for r in results)
        except Exception as e:  # one broken backup must not stop the queue for every backup after it
            b.update(status="failed", reason=f"runtime error: {e}"[:300])
        b["finished_at"] = time.time()
        _status(b)


def _targets(b):
    return frappe_target.targets(FRAPPE) if b.get("kind") == "frappe" else guard.apps()


@asynccontextmanager
async def lifespan(_):
    global queue
    guard.apps()  # a config that could restore into anything but a test instance stops the service here
    await guard.remove_orphans()
    queue = asyncio.Queue()
    for f in sorted(RESULTS.glob("*.json")) if RESULTS.exists() else []:
        b = json.loads(f.read_text())
        backups[b["id"]] = b
        if b["status"] in ("queued", "running"):
            queue.put_nowait(b["id"])
    tasks = [asyncio.create_task(watcher()), asyncio.create_task(worker())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="BackupAirlock", docs_url=None, redoc_url=None, lifespan=lifespan)


def _summary(b):
    m = b.get("manifest") or {}
    checks = [c for r in b["cells"].values() for c in r["checks"]]
    return {"id": b["id"], "kind": b.get("kind", "postgres"), "file": b["file"], "sha256": m.get("sha256"), "status": b["status"], "reason": b.get("reason", ""),
            "created_at": m.get("created_at"), "schema_version": m.get("schema_version"),
            "app_version": m.get("app_version"), "bytes": b.get("bytes"), "found_at": b.get("found_at"),
            "started_at": b.get("started_at"), "finished_at": b.get("finished_at"),
            "elapsed_s": round(b["finished_at"] - b["started_at"], 2) if b.get("finished_at") else None,
            "checks_passed": sum(c["pass"] for c in checks), "checks_total": len(checks),
            "cells": {v: {"state": r["state"], "reason": r["reason"], "elapsed_s": r["elapsed_s"]}
                      for v, r in b["cells"].items()}}


def _get(bid):
    if bid not in backups:
        raise HTTPException(404, "no such backup")
    return backups[bid]


@app.get("/health")
def health():
    return {"ok": True, "backups": len(backups), "queued": queue.qsize() if queue else 0,
            "app_versions": [a["version"] for a in guard.apps()],
            "frappe": {"source": f"{FRAPPE['source']['site']} (read only)", "restores_into": [t["site"] for t in FRAPPE["tests"]]}
            if FRAPPE else None}


@app.get("/api/backups")
def list_backups():
    return sorted((_summary(b) for b in backups.values()), key=lambda s: s["id"], reverse=True)


@app.get("/api/backups/{bid}")
def backup_detail(bid: str):
    b = _get(bid)
    logs = {}
    for v in b["cells"]:
        f = RESULTS / f"{bid}@{v}.events.jsonl"
        logs[v] = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
    return {**_summary(b), "manifest": b.get("manifest"), "cells": b["cells"], "logs": logs,
            "certificate": b.get("certificate")}


@app.get("/api/backups/{bid}/certificate.json")
def certificate(bid: str):
    cert = _get(bid).get("certificate")
    if not cert:
        raise HTTPException(409, "not finished")
    return JSONResponse(cert, headers={"Content-Disposition": f'attachment; filename="dr-certificate-{bid}.json"'})


@app.get("/api/backups/{bid}/incident.json")
def incident(bid: str):
    """Forensic log of a quarantined backup: what the static scan found and what the sandbox stopped."""
    b = _get(bid)
    if b["status"] != "quarantined":
        raise HTTPException(409, f"backup is {b['status']}, not quarantined")
    cells = []
    for v, r in b["cells"].items():
        f = RESULTS / f"{bid}@{v}.events.jsonl"
        events = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
        cells.append({"app_version": v, "restored_into": r["restored_into"], "signals": r["signals"],
                      "static_scan": r["static_scan"],
                      "containment_events": [{k: e.get(k) for k in ("t", "signal", "event", "detail")}
                                             for e in events if e["type"] == "policy"],
                      "errors": [e["line"] for e in events if e["type"] == "log" and "ERROR" in e["line"]][:20]})
    return JSONResponse({"backup": b["file"], "sha256": b["manifest"]["sha256"], "status": b["status"],
                         "reason": b["reason"], "cells": cells},
                        headers={"Content-Disposition": f'attachment; filename="incident-{bid}.json"'})


@app.get("/api/backups/{bid}/bridges/{name}")
def bridge(bid: str, name: str):
    for r in _get(bid)["cells"].values():
        for x in r["bridges"]:
            if x["name"] == name:
                return PlainTextResponse(x["sql"])
    raise HTTPException(404, "no such bridge")


@app.post("/api/backups/{bid}/rerun")
def rerun(bid: str):
    b = _get(bid)
    if b["status"] in ("queued", "running"):
        raise HTTPException(409, f"backup is {b['status']}")
    if not b.get("manifest"):  # unreadable when found (e.g. read mid-write): the manifest may be fixed by now
        try:
            b.update((frappe_target if b.get("kind") == "frappe" else guard).load_backup(b["path"]))
        except ValueError as e:
            raise HTTPException(409, f"backup is {b['status']}: {e}") from None
    b.update(status="queued", reason="", cells={}, certificate=None, started_at=None, finished_at=None)
    _status(b)
    queue.put_nowait(bid)
    return {"queued": bid}


@app.get("/api/matrix")
def matrix():
    versions = [a["version"] for a in guard.apps()]
    rows = sorted(backups.values(), key=lambda b: b["id"], reverse=True)
    return {"app_versions": versions + ([t["site"] for t in FRAPPE["tests"]] if FRAPPE else []),
            "backups": [{"id": b["id"], "schema_version": (b.get("manifest") or {}).get("schema_version"),
                         "status": b["status"],
                         "cells": [{"app_version": v, "state": b["cells"][v]["state"], "reason": b["cells"][v]["reason"]}
                                   if v in b["cells"] else {"app_version": v, "state": b["status"] if b["status"] in
                                                            ("queued", "running") else "not_run", "reason": ""}
                                   for v in [a["version"] for a in _targets(b)]]} for b in rows]}


@app.get("/api/stream")
async def stream():
    q = asyncio.Queue()
    subscribers.add(q)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("RESTOREGUARD_HOST", "127.0.0.1"), port=int(os.environ.get("RESTOREGUARD_PORT", "8600")))
