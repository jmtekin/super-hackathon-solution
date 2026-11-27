"""Run one restore attempt in a disposable, locked-down container.

The whole product rests on this: a backup is data plus executable assumptions,
so nothing from it may run in the control plane. KVM is not available on the
build host, so the sandbox is hardened Docker rather than a microVM: no network,
capped CPU/memory/pids, read-only root, the backup mounted read-only, a tmpfs
scratch dir, all capabilities dropped, an unprivileged uid, and a wall-clock
limit enforced from outside. The only "secret" inside is a honey file
(/root/.env, planted in the image): a backup that prints it has tried to steal
credentials. Containment is reported as signals (network, rootfs, honey,
memory, timeout) read from the sandbox's own output and its cgroup, the moment
they happen. A fresh container per attempt; it is removed in
every outcome and the removal is checked, not assumed.

Only stdout, stderr, the exit code and one artifact (the restored database)
come back. The artifact travels as a base64 line on stdout so no writable host
path is ever mounted into the sandbox.
"""
import asyncio
import base64
import os
import re
import time
import uuid
from pathlib import Path

IMAGE = os.environ.get("RESTORER_IMAGE", "restorer-sandbox:2")
DOCKER = os.environ.get("RESTORER_DOCKER", "docker").split()
CPUS, MEMORY, PIDS = "1", "512m", "64"
MAX_STDOUT = 16 * 1024 * 1024
ARTIFACT_TAG = "@@ARTIFACT "
# (signal, what the sandbox printed, what the page says)
SIGNS = [
    ("network", re.compile(r"Network unreachable|Network is unreachable|bad address|Name does not resolve|"
                           r"Temporary failure in name resolution|Could not resolve", re.I),
     "NETWORK: DENIED, no interface but loopback, 0 bytes left the sandbox"),
    ("rootfs", re.compile(r"Read-only file system", re.I), "ROOTFS: READ-ONLY, write outside /work blocked"),
    ("honey", re.compile(r"AKIAHONEYAIRLOCK|honey/airlock|honey-airlock"),
     "HONEY FILE READ: /root/.env is a decoy, no real secret is in the sandbox"),
]

# The wrapper is ours, not the model's: it runs the model's script and ships
# the artifact back, so the artifact path never depends on the model.
WRAPPER = f"""cd /work
sh /job/restore.sh
rc=$?
if [ -f /work/restored.db ]; then echo "{ARTIFACT_TAG}restored.db $(base64 -w 0 /work/restored.db)"; fi
exit $rc
"""


def policy(timeout):
    return {"runtime": "docker (hardened, no KVM on build host)", "network": "none", "cpus": CPUS,
            "memory": MEMORY, "pids_limit": int(PIDS), "rootfs": "read-only", "input": "read-only",
            "workdir": "tmpfs 64 MB", "capabilities": "all dropped", "user": "65534 (nobody)",
            "secrets": "none (honey file /root/.env only)", "timeout_s": timeout}


def _cgroup(cid):
    for p in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope", f"/sys/fs/cgroup/docker/{cid}"):
        if os.path.isdir(p):
            return Path(p)
    return None


def _read_usage(cg):
    try:
        usec = int(next(l for l in (cg / "cpu.stat").read_text().splitlines() if l.startswith("usage_usec")).split()[1])
        mem = int((cg / "memory.current").read_text())
        ooms = int(next(l for l in (cg / "memory.events").read_text().splitlines() if l.startswith("oom_kill")).split()[1])
        return usec, mem, ooms
    except (OSError, StopIteration, ValueError):
        return None


async def _docker(*args):
    p = await asyncio.create_subprocess_exec(*DOCKER, *args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="replace").strip()


async def run(input_dir, script, workdir, timeout, emit, files=None):
    """Run `script` against `input_dir`; `emit(event)` gets live events. Returns the result dict.

    `files` ({name: text}) are extra job files, mounted read-only at /job/<name> next to restore.sh."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    job = {"restore.sh": script, "wrapper.sh": WRAPPER, **(files or {})}
    for f, text in job.items():
        (workdir / f).write_text(text)
        (workdir / f).chmod(0o644)
    name = f"restorer-{uuid.uuid4().hex[:10]}"
    cidfile = workdir / "cid"
    args = ["run", "--rm", "--name", name, "--cidfile", str(cidfile),
            "--network", "none", "--cpus", CPUS, "--memory", MEMORY, "--memory-swap", MEMORY,
            "--pids-limit", PIDS, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534", "--tmpfs", "/work:rw,exec,size=64m,uid=65534",
            "--tmpfs", "/tmp:rw,size=16m", "-v", f"{Path(input_dir).resolve()}:/input/backup:ro",
            *(a for f in job for a in ("-v", f"{workdir.resolve()}/{f}:/job/{f}:ro")),
            IMAGE, "sh", "/job/wrapper.sh"]
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(*DOCKER, *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, limit=MAX_STDOUT)
    emit({"type": "sandbox", "phase": "started", "name": name, "policy": policy(timeout)})
    out, err, artifact, signals = [], [], None, set()

    def signal(sig, event, detail=""):
        if sig not in signals:
            signals.add(sig)
            emit({"type": "policy", "signal": sig, "event": event, "detail": detail[:300]})

    async def pump(stream, label, sink):
        nonlocal artifact
        while line := await stream.readline():
            text = line.decode(errors="replace").rstrip("\n")
            if label == "stdout" and text.startswith(ARTIFACT_TAG):
                _, fname, b64 = text.split(" ", 2)
                artifact = (fname, base64.b64decode(b64))
                continue
            sink.append(text)
            emit({"type": "log", "stream": label, "line": text[:2000]})
            for sig, pattern, event in SIGNS:
                if pattern.search(text):
                    signal(sig, event, text)

    async def sample():
        cg, last, peak = None, None, 0.0
        while True:
            await asyncio.sleep(0.25)
            if cg is None and cidfile.exists() and (cid := cidfile.read_text().strip()):
                cg = _cgroup(cid)
            if cg is None or (u := _read_usage(cg)) is None:
                continue
            now = time.monotonic()
            if last:
                pct = (u[0] - last[0]) / ((now - last[1]) * 1e6) * 100
                peak = max(peak, pct)
                emit({"type": "cpu", "pct": round(min(pct, 100 * float(CPUS)), 1), "cap": 100 * float(CPUS),
                      "mem_mb": round(u[1] / 2**20, 1), "elapsed": round(now - t0, 2)})
            if u[2]:
                signal("memory", f"MEMORY CAP HIT: {MEMORY[:-1]} MB, the kernel killed the runaway process")
            last = (u[0], now)

    sampler = asyncio.create_task(sample())
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.gather(pump(proc.stdout, "stdout", out), pump(proc.stderr, "stderr", err),
                                              proc.wait()), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        signal("timeout", f"TIMEOUT: {timeout} s wall clock reached, terminating sandbox")
        await _docker("kill", name)
    finally:
        sampler.cancel()
        await _docker("rm", "-f", name)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
    elapsed = round(time.monotonic() - t0, 2)
    rc, _ = await _docker("inspect", name)
    destroyed = rc != 0
    exit_code = "timeout" if timed_out else proc.returncode
    if exit_code == 137:  # SIGKILL from the OOM killer when the cgroup sample missed it
        signal("memory", f"MEMORY CAP HIT: {MEMORY[:-1]} MB, the kernel killed the runaway process")
    emit({"type": "sandbox", "phase": "exited", "name": name, "exit": exit_code, "elapsed": elapsed})
    emit({"type": "sandbox", "phase": "destroyed" if destroyed else "NOT DESTROYED", "name": name})
    cidfile.unlink(missing_ok=True)
    return {"name": name, "exit": exit_code, "elapsed": elapsed, "stdout": "\n".join(out)[-8000:],
            "stderr": "\n".join(err)[-8000:], "artifact": artifact, "network_denied": "network" in signals,
            "timed_out": timed_out, "destroyed": destroyed, "signals": sorted(signals)}
