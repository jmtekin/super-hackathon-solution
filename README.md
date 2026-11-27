# Restorer

Know your backup works before the outage does. A restore drill turns a backup
plus a recovery goal into a verified **Recovery Proof**; every restore attempt
runs in a disposable sandbox, never in the control plane.

Live demo: https://vps.taild101c7.ts.net:10000/restorer/

## How a drill runs

1. The backup is fingerprinted (sha256) and mounted read-only.
2. Vultr Serverless Inference (`glm-5.3`, fallback `deepseek-v4.1-flash-1017`) writes a plan and a restore script from the backup's catalog.
3. A fresh container runs it: `--network none`, 1 CPU, 256 MB, 128 pids, read-only root, tmpfs workdir, all capabilities dropped, uid nobody, wall-clock timeout. It is removed afterwards and the removal is checked.
4. On failure, stderr goes back to the model, which repairs the script (diff shown); a new sandbox runs it. Up to 3 attempts.
5. Deterministic checks, no model: row counts, sampled row hashes, schema assertions, one app smoke query, against the manifest recorded at backup time (kept in the control plane, never in the backup).
6. The model explains the evidence; the verdict itself comes only from steps 3 to 5.

**Backup chain** (fixture 4): every nightly backup is restored natively into the current app in parallel sandboxes; for nights on an older schema the model reads old vs current schema and writes one `migrate.sql`; the nights are restored into their own schema and migrated, one sandbox each, in parallel; every round is checked per night against a manifest built by a reference migration in `fixtures/build.py` (row counts, sampled row hashes, a sha256 over every row, schema incl. `user_version`, smoke query). A sandbox error or a failed check goes back to the model for a repair, up to 3 rounds. The restore scripts are the app's own; only the migration is generated.

Fixtures (`fixtures/build.py`, deterministic): **recoverable** (catalog points to a stale path, first attempt fails, repair succeeds), **corrupted** (restores cleanly, orders silently truncated), **compromised** (post-restore hook calls out to the network, then spins: network denied, CPU at cap, killed at 3 s), **drift** (10 nightly data-only backups; after night 9 `users` became `customers`, `name` split into `first_name`/`last_name`, `orders.currency NOT NULL DEFAULT 'SEK'` added: nights 1-9 fail natively with "table orders has 7 columns but 6 values were supplied" and are migrated v1 to v2, night 10 restores natively).

## Files

| File | What |
|---|---|
| `app.py` | FastAPI control plane: drill pipeline, LLM calls, SSE events, proof downloads |
| `sandbox.py` | hardened Docker runner, live CPU/memory from the container cgroup |
| `verify.py` | the deterministic checks (four, plus table digests when the manifest has them) |
| `static/index.html` | the single-page UI (relative paths, works under any prefix) |
| `fixtures/build.py` | builds the four fixtures + their manifests, incl. the reference v1 to v2 migration |
| `sandbox.Dockerfile` | sandbox image (alpine + sqlite) |
| `install.sh` | one-command install on a Vultr VM |
| `shoot.js` | headless end-to-end run of all fixtures with screenshots into `shots/` |

## Deploy on a Vultr VM

1. Create a Cloud Compute VM, Ubuntu 24.04, 2 vCPU / 4 GB is plenty. Open port 80 in its Vultr firewall group (if one is attached).
2. Copy this directory to the VM, e.g. `scp -r restorer root@<vm-ip>:/root/` (or `git clone` it).
3. On the VM, as root:
   ```sh
   cd /root/restorer
   VULTR_INFERENCE_KEY='<your key>' sh install.sh
   ```
   It installs Docker and Python, builds the sandbox image and fixtures, writes the key to `/etc/restorer.env` (mode 600), and starts `restorer.service` on port 80 (`RESTORER_PORT=8530 sh install.sh` for another port).
4. Open `http://<vm-ip>/`. Health: `http://<vm-ip>/health`.

Env vars: `VULTR_INFERENCE_KEY` (required), `RESTORER_HOST` (default 127.0.0.1; install.sh sets 0.0.0.0), `RESTORER_PORT` (default 8530), `RESTORER_DOCKER` (default `docker`), `RESTORER_IMAGE` (default `restorer-sandbox:1`).

## Limits (honest)

- Sandbox is hardened Docker, not a microVM: the build host has no KVM. On a VM with nested virtualisation, swap `sandbox.py`'s `docker run` for Microsandbox/Firecracker; nothing else changes.
- The control plane talks to the Docker daemon, which is root-equivalent; it is never exposed to the sandbox.
- One drill at a time (409 otherwise); a chain drill runs up to 10 sandboxes at once inside it. No auth, by design for the demo.
- The chain builds one migration per run and refuses a chain whose old nights span more than one old schema.
