# RestoreGuard runtime

Proves every backup of the Nordvik Handel ERP restores into a working app: each new `pg_dump -Fc` + manifest
(contract: `../restorer/handoff/PROMPT-FOR-MOHAMED.md`) is restored into a throwaway **test instance** (its own
Postgres 16 container, no network, the app version under test in that container's network namespace, never the
production app or its database), checked against its manifest and the business rules, booted, and probed. A
schema/app mismatch gets a GLM-5.3 bridge migration (Vultr Serverless Inference), retried up to 3 rounds. Results:
JSON API + SSE for Jonas's UI, and BackupAirlock's DR Readiness Certificate per backup.

API: `../restorer/README.md`, section **Runtime API** (the UI is built against it; shapes only ever grow).

## One cell = one backup on one app version

| Step | Fails as |
|---|---|
| sha256 of the dump vs manifest | `checksum mismatch` |
| `pg_restore -f -` (no execution): TOC + data readable, static scan for COPY PROGRAM, untrusted languages, file access, dblink | `dump unreadable` / quarantine |
| restore into the test instance's Postgres (`--exit-on-error`), server log read for containment signals | `restore failed` / quarantine |
| `contract/manifest.sql`: 9 row counts + 3 invariant sums vs the manifest | `row counts differ`, `sums differ` |
| `contract/rules.sql`: invoice = lines x (1 + VAT), paid invoice = payments, stock = moves, 6 FK checks, admin bcrypt row | `rule broken (<rule>: n rows, e.g. id)` |
| boot the app image on it: `/health` (ok, its schema), `/api/dashboard` counts = manifest, login right 200 / wrong 401; contract again after the app's own migrations | `app does not run on the restored data` |
| mismatch: GLM writes in-place SQL to the app's schema (its migrations read from its image), app rebooted, contract re-run; up to 3 rounds | `healed` or `no working bridge` |

States: `native`, `healed`, `failed`, `quarantined` (any network/rootfs/honey/memory signal or static-scan finding).
Sandbox: `--network none`, 1 CPU at cpu-shares 256, 384 MB (app 128 MB), pids 128, read-only root, dump read-only,
data on a 200 MB tmpfs, all capabilities dropped, uid 70, 180 s per cell, removed in every outcome (checked). One
sandbox at a time box-wide (`state/sandbox.lock`), so the fault kit and the service never overlap.
`apps.json` lists the app versions (image, schema, port, migrations path); any key or value that could point a
restore at a configured database or host is refused at startup (`guard.check_app`, tests in `tests/`).

## Files

| File | |
|---|---|
| `guard.py` | the cell: sandbox, checks, app probe, bridge loop, certificate |
| `api.py` | service: watcher (folders, or mirror of the app's `/backups/` HTTP endpoint), queue, results API, SSE |
| `contract/manifest.sql`, `contract/rules.sql` | the contract's numbers and rules; the backup job and RestoreGuard run the same SQL |
| `faults.py` | fault-injection kit + table runner |
| `standin/` | **STAND-IN** for Mohamed's app (same tables, rules, endpoints, migrations, backup job); replaced by his |
| `apps.json` | app versions under test |
| `restoreguard.service`, `install.sh` | this VPS / a Vultr VM |

## Run

This VPS (`restoreguard.service`, 127.0.0.1:8600, watches `backups/` and `faults/out/`):
```sh
sh standin/run_here.sh backups            # STAND-IN: seed, traffic, backup v1, upgrade to 2.0.0, traffic, backup v2 (~75 s)
python faults.py backups/<ts>_v1.dump     # makes 6 faulty copies in faults/out/, runs them, prints the table
python -m unittest tests/test_guard.py
```
Vultr VM (Ubuntu 24.04, `restorer` checked out next to this directory):
`VULTR_INFERENCE_KEY=... [RESTOREGUARD_BACKUPS_URL=https://<app-vm>/backups/ BACKUPS_TOKEN=...] [ADMIN_PASSWORD=...] sh install.sh`.
The stand-in stack itself: `cd standin && cp .env.example .env && docker compose up -d` (backups every 30 min).

Fault kit result 2026-11-27 14:02 (backup `20261127T124924Z_v1`, both app versions):

| fault | must | caught by |
|---|---|---|
| bitflip, manifest stale | not restorable | sha256 matches manifest |
| bitflip, re-signed | not restorable | dump readable (`could not uncompress data: incorrect data check`) |
| truncated at 60 %, re-signed | not restorable | dump readable (`end of file`) |
| all payments deleted, re-signed | not restorable | row counts + sums + rule `paid invoice = sum of payments` |
| schema v2 (migration 002) | drift caught, DR ready | 1.0.0 refuses, GLM bridge round 1; 2.0.0 native |
| poisoned (CHECK constraint -> COPY TO PROGRAM wget + write /etc) | quarantined | static scan + network + rootfs signals |

## Limits

Docker, not a microVM (no KVM on this VPS). The login probe needs the app's seeded admin password
(`RESTOREGUARD_ADMIN_PASSWORD_FILE`). The HTTP mirror expects the listing at `RESTOREGUARD_BACKUPS_URL` to name
`<stem>.json` files (JSON or an HTML index both work) and `<stem>.dump`/`<stem>.json` under it, bearer token.
