# Spec checklist: console v4 (2026-11-27)
Covers: static/index.html (console v4), the UI routes in app.py (`GET /api/runs`, `/rg/*` proxy), design/shots-v4/.
Spec = BACKUPAIRLOCK-SPEC.md (teammate's), plus Jonas's 7-step plan (RESTOREGUARD-PLAN-JONAS.md) and George's
13:55 relay ("you cant restore into prod"). Two tabs, one vocabulary:
**Backups** = the RestoreGuard runtime (projects/restoreguard, watched ERP backups, Postgres) via `/rg/api/*`;
**Drill** = the spec's demo engine in this app (3 preloaded dumps + upload x ERP app v1.0/v2.0, SQLite).

| Spec line | Where in the UI | State |
|---|---|---|
| Visual Backup x App Version Compatibility Matrix | Both tabs: rows = backups (file, time, schema, size, sha), columns = app versions | done |
| "green/yellow/red grid" | Mark + word per cell; green = ready (native or healed), amber = drift / not restorable, red = quarantined only | done, deliberately not red for drift (brief: red = danger) |
| Select or upload backups | Drill: "Add .sql" (raw upload, 25 MB) joins the next drill as a 4th row | done; picking a subset of backups/app versions: missing (API runs all) |
| Select target app versions | Columns come from the runtime (`/rg/health` app_versions) | display only; no picker (API has none) |
| Business Logic Validation Suite: auth, ledger math, FK, API smoke | Cell panel groups: Manifest and restore / Business logic / App boot; each check with detail; all-pass groups fold to one line, a failing group opens | done |
| Self-healing bridge loop, "RED (Drift) -> YELLOW -> GREEN (Healed)" | Cell goes Running -> Drift healing (round n) -> Ready healed live; panel shows the bridge's changes, model, latency | done (live states from SSE) |
| Downloadable .sql patch | Panel: "Download <bridge>.sql" + sha256 (both tabs) | done |
| Host sends only schema diff + stderr to Vultr Inference | Panel/certificate list each model call (purpose, model, latency, tokens) | done (content of the prompt not shown) |
| Containment moment: click "Verify: backup_poisoned.sql" | Drill: "Run drill" runs all three incl. the poisoned dump; select the poisoned cell | partial: no single-backup verify button (API has no per-backup drill) |
| Three containment signals (network air-gap, read-only rootfs, memory/timeout cap, destroyed) | Panel "Containment": time, signal sentence, evidence line; sandbox destroyed + exit code; static-scan lines of the dump | done, as marked text lines, not flashing badges (the "childish" note) |
| "QUARANTINED, DO NOT RESTORE TO PROD" | Row verdict "Quarantined, do not restore" + headline sentence in red | done |
| Export forensic incident log | Drill panel "Incident log (JSON)" | done; Backups tab: runtime has no incident endpoint yet |
| Host stays responsive | Page keeps working during a run (SSE, other tab, certificate) | done |
| SHA-256 DR Readiness Certificate (backup hash, passing versions, test logs, bridge) | `#cert` view: backup x version table with result, bridge + sha, check counts, sandbox policy, model calls, proof sha256; Export JSON, Export HTML (drill), Print | done |
| Live SSE telemetry | Backups: `/rg/api/stream` (cells fill as checks arrive, "live" mark); Drill: `/api/runs/{id}/events` | done |
| Resource telemetry (CPU) | Not shown (drill emits cpu samples; the runtime does not) | missing, on purpose for now: no chart on a status screen |
| Sandbox flags (network none, read-only, caps, 512 MB, pids, 20 s) | Panel "Sandbox" line + certificate "Sandbox policy" | done |
| Plain-English rule -> assertion (optional) | none | missing (not in either engine) |
| Jonas step 5: dashboard that shows the backups | Backups tab = every watched backup, newest first, with time, schema, size, verdict | done |
| Jonas step 7: schema change, bit flips | Shown as rows when the runtime's fault kit drops them into backups/ | done: the fault kit's rows (truncated, rows deleted, poisoned, bit flip, stale bit flip, schema v2) show with their reasons (shots backups-*) |
| George 13:55: restore never into prod, target explicit | Panel line "restored into: test instance <sandbox>, destroyed after the checks"; header "each restored into a throwaway test instance, never production"; no restore-to-prod action anywhere; "(production)" column label removed | done: Backups reads the runtime's `restored_into` (instance, Postgres sandbox, `production_touched: false` -> "production untouched"); Drill names the sandbox that held the test instance |
| Rerun a backup | Backups panel "Run again" (POST rerun via proxy) | done |
| Phone layout | <=900 px: matrix stacks per backup, two cells side by side with the app name in each cell, panel below and scrolled to on tap; certificate table as stacked rows; no horizontal scroll at 390 | done |

API changes I would ask the runtime for (not made; its code is not mine):
- `sha256` in `/api/backups` summaries (now only in the detail).
- An incident endpoint for quarantined backups (the drill has `/incident.json`).
