# BackupAirlock

Every ERP backup is proved restorable before anyone needs it: each new backup is restored into a
throwaway, network-less test instance, checked against the source's own counts and business rules,
and run against every app version. Schema drift between backup and app is bridged by a migration a
Vultr Serverless Inference model writes and the checks then verify; a poisoned dump is quarantined.
Nothing is ever restored into production.

Vultr Agent Runtime Hackathon, 27 Nov 2026. ERP: Abdullah (Frappe/ERPNext on his Vultr VM). Runtime and
tests: George. UI: Jonas. Team spec: [docs/BACKUPAIRLOCK-SPEC.md](docs/BACKUPAIRLOCK-SPEC.md).

## Architecture

```
 ERP VM (Frappe 16 + ERPNext)                     BackupAirlock VM (Vultr, this repo)
 ┌──────────────────────────────┐   GET only     ┌───────────────────────────────────────────────┐
 │ demo.local :80  PRODUCTION   │ ─────────────► │ restoreguard/api.py  watcher + queue, :8600    │
 │   Backups page, /backups/*,  │  backup .sql.gz│   guard.py / frappe_target.py: one cell =      │
 │   get_count (manifest)       │  + counts      │   checksum -> static scan -> restore in sandbox │
 │                              │                │   (Postgres 16 / MariaDB 11.8, --network none, │
 │ test.local :8080  TEST COPY  │ ◄───────────── │   read-only root, mem/pid caps) -> counts +    │
 │   only site ever written     │  token API     │   rules -> app boot probe -> replay on test    │
 └──────────────────────────────┘                │        │ drift: GLM-5.3 writes bridge SQL,     │
                                                 │        │ re-verified, up to 3 rounds           │
                                                 │        ▼                                       │
 Browser ── :80 ───────────────────────────────► │ ui/app.py  console + drill, /rg/* read-only    │
                                                 │   proxy to the runtime, certificates, incidents│
                                                 └──────────────────┬────────────────────────────┘
                                                                    │ OpenAI-compatible
                                                                    ▼
                                                   Vultr Serverless Inference (api.vultrinference.com)
```

The verdict comes from the sandbox outcome and the checks only; the model writes restore scripts,
bridge migrations and prose, never the verdict.

## Layout

| Path | What |
|---|---|
| `restoreguard/` | the runtime: `api.py` (watcher, queue, JSON/SSE API), `guard.py` (Postgres cell), `frappe_target.py` (Frappe target), `faults.py` (fault kit), `contract/` (manifest + rules SQL shared with the backup job), `standin/` (stand-in Nordvik app until the real ERP replaced it). Details: [restoreguard/README.md](restoreguard/README.md) |
| `ui/` | the console (`static/index.html`: Backups tab = runtime, Drill tab = compatibility matrix) and its FastAPI app with the BackupAirlock drill, hardened sandbox runner and fixtures. Details: [ui/README.md](ui/README.md) |
| `tests/` | unit tests of the parts that need no sandbox (production refusal, malformed/missing/unreadable backups, every rule's failure case); `tests/e2e/` end-to-end suite against the running runtime |
| `e2e.sh`, `demo.sh` | e2e suite (20 tests, ~3.5 min, one command); live demo run (~3 min, see Demo script) |
| `docs/` | team spec, pitch, UI spec checklist, design references and screenshots |
| `install.sh`, `.env.example` | one-VM install |

## Run on one Vultr VM

Ubuntu 24.04, 2 GB RAM or more, as root:

```sh
git clone https://github.com/jmtekin/super-hackathon-solution.git && cd super-hackathon-solution
cp .env.example .env    # fill in VULTR_INFERENCE_KEY, and the FRAPPE_* lines for the Frappe target
sh install.sh
```

It installs Docker and Python, builds the sandbox and stand-in images, pulls `postgres:16-alpine` and
`mariadb:11.8`, and starts `restorer.service` (console, public on `RESTORER_PORT`, default 80) and
`restoreguard.service` (runtime, `127.0.0.1:8600`). Secrets go from `.env` into `/etc/restorer.env` and
`/etc/restoreguard/` (mode 600) and never into a sandbox. `.env` is gitignored.

Frappe target: set `FRAPPE_SOURCE_URL`/`FRAPPE_TEST_URL` and the two `api_key:api_secret` tokens. The
runtime refuses to start the target if the test URL is on port 80, equals the source, or names
`demo.local`. Without `FRAPPE_SOURCE_URL` it runs on the stand-in backups only.

Tests, from the repo root: `python3 -m unittest tests/test_guard.py tests/test_frappe.py` (needs
`ui/requirements.txt` installed, since the runtime imports the console's model helpers).

## Demo script (about 3 minutes)

`./demo.sh` drives the Backups tab live: it drops a good backup (DR ready), a corrupted one (not restorable), a
schema-drift one (GLM bridge heals it on app 1.0.0) and a poisoned one (quarantined) into the runtime's watched folder,
one at a time, with `PAUSE` s (default 20) of talking time after each verdict; `./demo.sh reset` clears its copies
without restarting the runtime. Sources: the newest `restoreguard/backups/*_v1` and the fault kit's `faults/out/`
(set `RG_HOME` if the runtime lives elsewhere). Not in parallel with `./e2e.sh` or `faults.py`.


1. **The problem.** Open the console, Backups tab: every backup production made, each with a verdict.
   "Backups that were never restored are hopes, not backups."
2. **A real backup, proved.** Pick the newest Frappe backup: restored into a sandboxed MariaDB, counts
   for every doctype match production's, the Demo Service Request rules hold (`total_cost` = hours x
   rate), then replayed on `test.local`. Point at "restored into: test instance, production untouched".
3. **Drift healed.** Drill tab, Run drill: 3 backups x app v1.0/v2.0. The old backup fails natively on
   the new app (`no such column: move_type`), the model writes a bridge migration, the checks re-run in
   a fresh sandbox, the cell turns ready; the bridge SQL is downloadable.
4. **Poisoned dump.** Same matrix: the poisoned dump tries a network exfil, a rootfs write and a memory
   bomb; all are blocked, the cell is quarantined, the incident log names each signal.
5. **Fault kit.** `python3 restoreguard/faults.py <good backup>`: bit flip, truncation, deleted rows,
   `total_cost` x10, poisoned copy; the table shows every fault caught before it reaches `test.local`.
6. **Proof.** Download the DR Readiness Certificate (`proof.html`) for the run.
