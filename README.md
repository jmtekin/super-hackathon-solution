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

## Frappe target: Abdullah's ERP (`frappe_target.py`, `frappe.json`)

Frappe 16 + ERPNext + demo_app (doctype Demo Service Request) on 70.34.199.143: **demo.local :80 = production,
read only**; test.local :8080 = DB-isolated clone, the only site written to. Tokens: `.config/restoreguard-frappe.json`.
The watcher reads production's Backups page every 120 s, downloads each new `bench backup` sql.gz from `/backups/`
into `backups-frappe/` and writes its manifest last: `get_count` per doctype that has rows in the dump (minus the
self-growing logs in `frappe.json`), counted right after the download. Every call to the VM is serialised, 2/s max.

| Step | Fails as |
|---|---|
| sha256 vs manifest | `checksum mismatch` |
| gzip stream + `-- Dump completed` trailer; static scan of the SQL outside string literals (INTO OUTFILE, LOAD DATA/LOAD_FILE, SONAME, `\!`, GRANT/CREATE USER/SET GLOBAL, triggers/events) | `dump unreadable` / quarantine |
| full site restore into a MariaDB 11.8 sandbox (network none, 512 MB incl. its tmpfs data dir, read-only root, uid 999) | `restore failed` |
| row counts: Demo Service Request + every counted doctype vs the manifest | `rows match the source` |
| rules from the dump's own DocField: required fields, Select options, Link targets exist; total_cost = estimated_hours x hourly_rate; hours > 0, rate >= 0 | `rule: <rule>` |
| test.local: `get_logged_user` 200; its Demo Service Requests made exactly the backup's (PUT by name, POST missing, DELETE test-only; the before-state goes to the event log, Frappe keeps a Deleted Document); read back, compared, `get_count`, rules again | `test.local holds the backup's N records` |

API additions (not yet in `../restorer/README.md` Runtime API): `kind` (`postgres`|`frappe`) in every backup
summary; Frappe backups have one cell, keyed `test.local`, whose `restored_into` is `{instance: "test.local on
70.34.199.143:8080, production untouched", database, app, production}`; `/health` has `frappe: {source,
restores_into}`; `/api/matrix` lists `test.local` as a column, cells only where they apply; SSE adds `replay` events.
A backup that fails any sandbox check never reaches test.local. Refused at startup (`check_config`): a test target on
port 80, on production's host:port, or named like production; the production client cannot build a non-GET request.
No shell on the VM, so `bench --site test.local restore` (whole site on test.local) is not used: the whole site is
proved in the sandbox, the business doctype on test.local. A record test.local lacks is re-created and named by the
app's naming series (not renamable); the check names the mapping.

Fault kit (`python faults.py backups-frappe/<stem>.sql.gz`, copies in `faults/out-frappe/`), 2026-11-27 14:31 on
`20261127_140556-demo_local-database`: 7/7 caught, none reached test.local.

| fault | caught by |
|---|---|
| bitflip, manifest stale | sha256 matches manifest |
| bitflip, re-signed | dump readable (gzip CRC) |
| sql.gz cut at 60 % | dump readable (end-of-stream marker) |
| SQL cut at 60 %, clean gzip | dump readable (no mysqldump trailer) |
| Demo Service Request rows deleted | rows match the source (0 vs 1) |
| total_cost x10 | rule total_cost = estimated_hours x hourly_rate |
| INTO OUTFILE /etc/cron.d + `\!` shell escape | quarantine: 2 scan findings + rootfs signal |

## Files

| File | |
|---|---|
| `guard.py` | the cell: sandbox, checks, app probe, bridge loop, certificate |
| `frappe_target.py`, `frappe.json` | Frappe/MariaDB target: read-only fetch from production, MariaDB sandbox, test.local replay |
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
python -m unittest tests/test_guard.py tests/test_frappe.py
```
Vultr VM (Ubuntu 24.04, `restorer` checked out next to this directory):
`VULTR_INFERENCE_KEY=... [RESTOREGUARD_BACKUPS_URL=https://<app-vm>/backups/ BACKUPS_TOKEN=...] [ADMIN_PASSWORD=...] sh install.sh`.
The stand-in stack itself: `cd standin && cp .env.example .env && docker compose up -d` (backups every 30 min).

Fault kit result 2026-11-27 ~14:00 (backup `20261127T124924Z_v1`, both app versions):

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
