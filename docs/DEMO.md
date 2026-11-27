# Demo: three scenarios

Open https://65-20-116-145.sslip.io/. The bottom band is the timeline of every backup the ERP made (oldest left); above it, the selected backup's checks in three groups, Verify / Heal / Restore, and the live run log. Each scenario button runs on the newest real ERP backup from production (`demo.local`). Runs are real: real sandboxes, real `bench restore` into the test sites, real model calls. Production is only read.

## 1. Tonight's backup

**Story.** The ERP made its nightly backup. Is it restorable?

**Press** "Tonight's backup".

**Watch.** Verify: sha256, gzip, static scan. Then the sandbox restore (MariaDB, no network) with a table count ticking, counts for 135 doctypes vs production, Demo Service Request rules (`total_cost = hours x rate`, required fields). Then the real restore: `bench --site test.local restore` streams its output, followed by live checks on test.local (login, token API, counts, every record read back).

**Result.** `native`, 25/25 checks, 36.8 s. The card above the point shows test.local's own ERP screen; click it to open the restored copy.

## 2. App upgraded

**Story.** The ERP was upgraded (demo_app 2.0.0: `estimated_hours` renamed to `effort_hours` with no migration patch, new required field `category`). Can last night's backup still be restored onto the new app?

**Press** "App upgraded".

**Watch.** The native restore into test2.local succeeds and `bench migrate` exits 0, but the checks fail: `effort_hours` is the default 1 and `category` is empty. Heal: GLM-5.3 on Vultr Serverless Inference reads both schemas and the failing checks and writes UPDATE-only SQL (shown in the log). It is policy-checked, tried in a sandbox against an emulation of migrate, then the VM restores again with it.

**Result.** `healed` in round 1, 28/28 checks, 56.7 s. The Overview shows the same backup before (failing) and after (restored).

## 3. Bad backup arrives

**Story.** A tampered backup lands next to the good ones.

**Press** "Bad backup arrives".

**Watch.** A copy of the newest backup with `INTO OUTFILE '/etc/cron.d/...'` and a client shell escape spliced in. The static scan flags both; the sandbox's read-only root blocks the write.

**Result.** `quarantined` in 8.5 s, never sent to a test site. The "latest restorable" tick stays on the good backup.

**Reset** removes the injected copies and puts back the results they replaced.

## Without the UI

- `./demo.sh` (about 3 minutes): the Postgres path, four states in turn: DR ready, not restorable (corrupted), healed (schema v2 backup on app 1.0.0), quarantined (CHECK constraint calling `COPY ... PROGRAM`).
- `python3 restoreguard/faults.py <good backup>`: every fault in a table with what caught it.
- `?view=drill` on the live site: the compatibility-matrix drill (3 dumps x 2 app versions, SQLite ERP, 14-19 s).
