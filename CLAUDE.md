# CLAUDE.md: map of this repo for an LLM reader

BackupAirlock proves ERP backups restorable before they are needed: Verify (checksum, static scan, sandbox restore, counts and business rules), Heal (a Vultr Serverless Inference model writes a bridge migration when the app was upgraded; deterministic checks judge it), Restore (only into test copies, never production). Built for the Vultr "Agent Runtime" hackathon, Stockholm, 27 Nov 2026. Live: https://65-20-116-145.sslip.io/

## Where things are

| Path | What it is | Entry point |
|---|---|---|
| `restoreguard/` | the runtime: watches backup sources, queues cells, runs them, serves JSON + SSE | `api.py` (FastAPI, 127.0.0.1:8600) |
| `restoreguard/guard.py` | one cell for a Postgres backup: checksum, scan, sandbox restore, contract SQL, app boot, GLM bridge loop, certificate | `run_cell` (`guard.py:344`) |
| `restoreguard/frappe_target.py` | the real ERP (Frappe 16 + ERPNext on Vultr): read-only production client, MariaDB sandbox, forced-command `bench restore` of test sites, MariaDB bridge | `run_cell` (`frappe_target.py:688`) |
| `restoreguard/faults.py` | fault kit: bit flip, truncation, deleted rows, wrong totals, poisoned copy; table of what caught each | CLI |
| `restoreguard/contract/` | manifest + business-rule SQL shared by the backup job and the runtime | |
| `restoreguard/standin/` | stand-in Nordvik ERP (Postgres, app 1.0.0 / 2.0.0) for the Postgres path | `run_here.sh` |
| `ui/app.py` | console server: pages, `/rg/*` proxy to the runtime, the matrix drill, Vultr inference client | FastAPI |
| `ui/static/` | the console: `index.html` (timeline + checks + Run), `overview.html` (flow, before/after heal, facts), `airlock.css` | |
| `ui/sandbox.py`, `ui/verify.py`, `ui/fixtures/`, `ui/apps/` | the compatibility-matrix drill (SQLite ERP, 3 dumps x 2 app versions) | |
| `tests/` | 49 unit tests; `tests/e2e/` 20 end-to-end test cases (14 functions, 3 parametrized) against a running runtime | `python3 -m unittest tests/test_guard.py tests/test_frappe.py`, `./e2e.sh` |
| `install.sh`, `.env.example` | one-command install on a Vultr VM (Docker, Caddy, both services) | `sh install.sh` |
| `demo.sh` | scripted 4-state Postgres demo | `./demo.sh` |
| `docs/` | ARCHITECTURE, SECURITY, DEMO, EVALUATION; `design/` current UI notes; `archive/` original pitch, spec, old shots | |

## Key claims and where the code proves them

| Claim | Evidence |
|---|---|
| All LLM calls go to Vultr Serverless Inference; GLM-5.3 primary, DeepSeek V4.1 Flash fallback | `ui/app.py:47-48`, `ui/app.py:148` (`llm`) |
| Production is never written: non-GET to production raises | `restoreguard/frappe_target.py:135-138` |
| A config pointing a test target at production (port 80, same host/site, `demo.local`) refuses to load | `restoreguard/frappe_target.py:99-110` |
| Stand-in app configs that could name a database host are refused | `restoreguard/guard.py:78-90` (`check_app`) |
| Sandbox: no network, read-only root, all caps dropped, memory/pid/CPU caps, non-root | `restoreguard/guard.py:229-231`, `restoreguard/frappe_target.py:303-305`, `ui/sandbox.py:96-97` |
| Static scan of dumps before any restore | `restoreguard/guard.py:53` (Postgres), `restoreguard/frappe_target.py:76`, `:179` (MariaDB) |
| Containment signals -> quarantine | `restoreguard/guard.py:58`, `:360` |
| Model output is policy-checked: bridge may only UPDATE the one doctype | `restoreguard/frappe_target.py:391-405` (`check_bridge`) |
| Heal loop, up to 3 rounds, re-verified each round | `restoreguard/guard.py:50`, `restoreguard/frappe_target.py:558` (`_bridge`) |
| A bridge is proved by data, not by the model's claim | `restoreguard/frappe_target.py:376` (`carried`) |
| Real restore of the test site over a forced-command SSH key, after the sandbox passed | `restoreguard/frappe_target.py:263` (`vm_restore`), `:638` (`_vm`) |
| The runtime polls production's Backups page every 120 s, read only, and runs every new backup unattended | `restoreguard/api.py:40`, `restoreguard/frappe_target.py:202` (`fetch_backups`) |
| Manifest = production's `get_count` for every doctype right after download | `restoreguard/frappe_target.py:228-230` |
| The verdict comes from sandbox outcomes and SQL checks, never the model | `restoreguard/guard.py:17-19` (design note), `:565` (status) |
| Model prompts: bridge writer + repairer (runtime), restore planner + repairer + report writer (drill) | `restoreguard/frappe_target.py:65-70`, `restoreguard/guard.py:62-69`, `ui/app.py:83-121` |
| Human approval is structural: nothing restores production; operators act on the certificate | `restoreguard/frappe_target.py:135-138`, certificate `restoreguard/guard.py:565` |
| Console proxies only reads, rerun and Run to the runtime | `ui/app.py:920-928` |

## Why the code has two parts

`restoreguard/` is the product's runtime (real ERP, Postgres stand-in, fault kit, API). `ui/` is the console the runtime is shown through, plus the first prototype it grew from: the compatibility-matrix drill on a small SQLite ERP, kept as a self-contained demo of the same idea (`?view=drill`). The runtime reuses the drill's Vultr inference client and containment signals (`restoreguard/guard.py:12-14`). The root `install.sh` runs `ui/install.sh` then `restoreguard/install.sh` and writes the Caddy config. "RestoreGuard" is the runtime's old internal name (unit `restoreguard.service`); the product is BackupAirlock.

## Measured results (details and reproduction: docs/EVALUATION.md)

- Real ERP backup, restored into `test.local` via `bench restore`: native, 25/25 checks, 36.8 s.
- Same backup on the upgraded app (`test2.local`, demo_app 2.0.0): healed by GLM-5.3 in round 1, 28/28 checks.
- Fault kit: 7/7 Frappe and 6/6 Postgres faults caught; none reached a test site. The Frappe faults are copies of a real production backup, not hand-made fixtures. Poisoned copy quarantined in 8.5 s.
- Drill matrix (3 dumps x 2 apps): 2 native, 2 healed round 1, poisoned quarantined with all 4 containment signals, 14-19 s.

## Vocabulary

cell = one backup on one app version; native = restores and passes as is; healed = passes after a model-written bridge; failed / not restorable; quarantined = containment signal or scan finding, never restored.
