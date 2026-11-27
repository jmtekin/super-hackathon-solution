# Teammate's spec for Restorer, verbatim (via Jonas WhatsApp 2026-11-27 12:55)

# **BackupAirlock**
## Core Value Proposition
**One-Line Pitch:**
**BackupAirlock** is an autonomous disaster-recovery verifier that restores database backups against multiple application codebase versions inside sealed, throwaway sandboxes—proving business-logic integrity, auto-generating bridge migrations for version drift, and quarantining poisoned dumps before they ever touch production.
### The 4 Real-World Problems It Solves
1. **Eliminates "Schrödinger's Backup":** Standard backup tools only verify that a `.sql` file exists or that `pg_restore` returns exit code `0`. They never prove that your actual application can boot and serve traffic on that data.
2. **Solves Schema-to-Codebase Version Drift (Your Key Differentiator):** When a company needs to restore a 3-month-old backup (`backup_v1.2`) onto today's production codebase (`app_v2.0`), the restore succeeds at the SQL layer—but the app crashes because columns were renamed, types changed, or constraints shifted. BackupAirlock automatically maps **which backup works with which codebase version** in a visual Compatibility Matrix.
3. **Proves Business Logic, Not Just Row Counts:** Instead of checking `COUNT(*) > 0`, BackupAirlock executes real domain invariants inside the sandbox (e.g., *Can a restored user authenticate? Does `SUM(line_items) == invoice_total`? Does the checkout API return `200 OK`?*).
4. **Zero-Trust Quarantine for Tampered/Ransomware Backups:** Restoring an untrusted or compromised SQL dump directly on a staging server is a known remote-code-execution vector (via shell-escape commands, malicious extensions, or recursive query bombs). BackupAirlock detonates every restore inside an air-gapped, zero-secret sandbox.
## 3. Full System Architecture (Vultr Containment-First)
Plaintext
#
```
[ React / Next.js Web Console ]  <-- Live SSE Telemetry & Compatibility Matrix
|
v
+-------------------------------------------------------------------+
|  HOST CONTROL PLANE (Single Vultr VM: vhp-4c-8gb-amd / VX1)       |
|  • FastAPI Orchestrator + Caddy (HTTPS)                           |
|  • Vultr Serverless Inference Client (API Key stays ONLY on host) |
|  • Deterministic Verifier & SHA-256 Receipt Ledger (SQLite)       |
|  • Codebase Version Registry (app_v1.0, app_v2.0) + Test Suites   |
+-------------------------------------------------------------------+
| (Spawns ephemeral sandbox per matrix cell)
v
+-------------------------------------------------------------------+
|  ISOLATED SANDBOX (Docker + gVisor `runsc` OR Microsandbox)       |
|  Flags: --network none | --read-only | --cap-drop ALL             |
|         --memory 512m  | --cpus 1    | --pids-limit 64 | 20s cap  |
|  Secrets: ZERO (only dummy honey-file /root/.env for trap)        |
|  1. Ephemeral SQLite / Postgres instance                          |
|  2. Loads untrusted backup.sql                                    |
|  3. Boots target app version + runs Business Logic Test Suite     |
|  4. Returns stdout / exit code / diff -> INSTANTLY DESTROYED      |
+-------------------------------------------------------------------+
```
### Component Breakdown
- **Central Control Plane (Host Vultr VM):** Runs the public web UI, manages uploaded/pre-loaded `.sql` backups and app codebase versions, schedules sandbox runs, and stores verifiable SHA-256 Disaster Recovery Certificates. **All Vultr API keys and real credentials live strictly here and never enter any sandbox.**
- **Vultr Serverless Inference (`https://api.vultrinference.com/v1`):**
- **Schema & Failure Analyst:** When `backup_v1` fails against `app_v2.0`, the host sends *only* the schema diff and test `stderr` to Vultr Serverless Inference.
- **Self-Healing Bridge Migration Author:** The LLM writes a targeted SQL bridge migration (e.g., renaming columns, backfilling defaults, casting types) to make the older backup compatible with the newer codebase.
- **Business-Logic Test Generator (Optional/Dynamic):** Translates plain-English domain rules from the user (e.g., *"Verify no negative account balances and every paid order has a customer"*) into executable Python/SQL assertions.
- **Ephemeral Sandbox Layer (`Docker + gVisor`):**
- Every cell in the **Backup × App Version Matrix** runs in its own fresh, air-gapped container (`-network none`, `-read-only`, `-tmpfs /work:size=64m`, 512 MB RAM, 1 vCPU, 64 PIDs, 20-second hard kill).
- Immediately destroyed after returning stdout, test pass/fail JSON, and resource telemetry.
## 4. End-to-End Execution Flow
1. **Ingest & Matrix Planning:** The user selects or uploads database backups (`backup_v1.sql`, `backup_v2.sql`, `backup_poisoned.sql`) and selects target application versions (`App v1.0`, `App v2.0`).
2. **Sandboxed Restore & Business Logic Execution:** For each `(Backup, App Version)` pair, the orchestrator boots a fresh gVisor container, restores the SQL dump into an ephemeral database, boots the target app's data layer, and runs the deterministic **Business Logic Validation Suite** (auth check, financial ledger math, foreign-key integrity, API smoke test).
3. **Self-Healing Bridge Migration Loop (Pattern A):**
- `backup_v1.sql` on `App v2.0` fails with `OperationalError: no such column: total_cents` (because v1 used `price_float`).
- The host feeds the error and schema diff to **Vultr Serverless Inference**, which generates `bridge_v1_to_v2.sql`.
- BackupAirlock spawns a **new** sandbox, applies `backup_v1.sql` + `bridge_v1_to_v2.sql`, re-runs the Business Logic Suite, and upgrades the matrix cell from **RED (Drift)** to **YELLOW → GREEN (Healed via Bridge Migration)** with a downloadable `.sql` patch.
4. **Verifiable Receipt:** The host mints a deterministic **DR Readiness Certificate** (SHA-256 hash of the backup, exact passing app versions, executed test logs, and any required bridge migration).
## 5. The Mandatory "Containment Moment" (20-Second Demo Script)
1. In the live web UI, click **"Verify: `backup_poisoned.sql`"** (simulating a ransomware-tampered or untrusted third-party dump).
2. Inside the air-gapped gVisor container, the dump attempts a **three-part attack**:
- **Credential Exfiltration:** Attempts a shell escape (`cat /root/.env` / outbound `curl` to an attacker endpoint).
- **Filesystem Tampering:** Attempts to write/delete outside `/work`.
- **Resource Bomb:** Triggers a runaway recursive SQL CTE (`WITH RECURSIVE bomb AS ...`) to exhaust CPU/RAM.
3. On screen, live SSE telemetry flashes three red containment badges:
- `NETWORK AIR-GAP ENFORCED (0 BYTES EXFILTRATED)`
- `READ-ONLY ROOTFS PROTECTED`
- `MEMORY/TIMEOUT CAP HIT (512 MB / 15s) · CONTAINER DESTROYED`
4. The host web app stays 100% responsive, marks `backup_poisoned.sql` as **QUARANTINED — DO NOT RESTORE TO PROD**, and exports the forensic incident log.
## 6. Fast-Track Build Scope (Remaining Time Today: 11:25–19:00 CET)
- **Build:**
- Single Vultr VM (`vhp-4c-8gb-amd`) + Caddy HTTPS + FastAPI + React/Tailwind UI.
- **3 pre-loaded SQL backups** (`v1_legacy.sql`, `v2_current.sql`, `poisoned_dump.sql`) + custom `.sql` upload.
- **2 real app versions** (`App v1.0`, `App v2.0`) with 4 deterministic business-logic tests (e.g., ledger sum invariant, user login hash check, active subscription check, API endpoint health).
- Visual **Backup × App Version Compatibility Matrix** (green/yellow/red grid).
- Visible **Self-Healing Bridge Migration** loop via Vultr Serverless Inference (`v1_legacy.sql` healed to pass `App v2.0`).
- Live **Containment Moment** telemetry panel + downloadable SHA-256 DR Certificate.
- **Intentionally Cut:** Live cloud RDS/S3 connectors, multi-gigabyte streaming dumps, and multi-tenant user auth. Lock scope at **16:00 CET** so you have a full hour to harden and record the demo before the 17:30–19:00 CET submission window.
