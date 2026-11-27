# Evaluation: measured results

Everything below was measured on 27 Nov 2026 on the Vultr VMs, on real runs (no mocks): real sandboxes, real `bench restore` into the ERP's test sites, real Vultr Serverless Inference calls. The live numbers are in `GET https://65-20-116-145.sslip.io/rg/api/backups` (public, read only).

## Real ERP (Frappe 16 + ERPNext, Abdullah's Vultr VM)

| Run | Target | Verdict | Checks | Time | Reproduce |
|---|---|---|---|---|---|
| Production backup 20261127_151409, "Tonight's backup" | test.local (demo_app 0.0.1) | native | 25/25 | 36.8 s | live page, scenario 1 |
| Same backup, "App upgraded" | test2.local (demo_app 2.0.0) | healed, GLM-5.3 round 1 (`effort_hours = estimated_hours`, `category` set) | 28/28 | 56.7 s | live page, scenario 2 |
| Poisoned copy, "Bad backup arrives" | test.local | quarantined (rootfs + 2 static-scan findings), never restored | 2/4, then stopped | 8.5 s | live page, scenario 3 |
| Production backup 20261127_140556 | test.local | native | 25/25 | 106.5 s (before test-site counts ran in parallel) | `POST /api/backups/{id}/rerun` |

Each ERP run = sandbox restore of the whole site + 135 doctype counts vs production + business rules + `bench restore` (and `bench migrate`) of the test site + the same counts, records and rules on the test site. Production was read only throughout: its Demo Service Request count and `modified` stamp were checked unchanged after the heal runs.

## Fault kit

`python3 restoreguard/faults.py <good backup>`: each fault is a copy of a good backup (for Frappe: a real backup downloaded from production) (the original is never touched). Corruptions get a re-signed manifest, so the checks past the checksum are exercised too. Exit 1 if any fault slips through.

| Path | Faults | Caught | Reached a test site |
|---|---|---|---|
| Frappe (MariaDB) | 7: bit flip, stale checksum, truncated `.gz`, truncated SQL, deleted rows, `total_cost` x10, poisoned | 7/7 | 0 |
| Postgres stand-in | 6: bit flip, stale checksum, truncation, deleted rows, schema v2, poisoned | 6/6 (schema v2 healed, the rest failed or quarantined) | 0 |

What caught each Postgres fault, from the live results: stale checksum -> `checksum mismatch`; bit flip -> `pg_restore: could not uncompress data`; truncated -> `could not read from input file`; rows deleted -> `row counts differ (payments: 0 vs 27058)` + `paid invoice = sum of payments` rule; poisoned -> containment `network, rootfs` + static scan.

## Compatibility-matrix drill (SQLite ERP, `?view=drill`)

3 dumps x 2 app versions, each cell its own sandbox, app booted, business-logic suite (`ui/apps/suite.py`: API health, login hash, ledger + invoice math, FK check), control-plane re-check of the books.

| Dump | App v1.0 | App v2.0 |
|---|---|---|
| `v1_legacy.sql` | native | healed (GLM bridge, round 1) |
| `v2_current.sql` | healed (bridge v2 -> v1) | native |
| `poisoned_dump.sql` | quarantined | quarantined: network denied (0 bytes out), read-only rootfs, honey read, memory cap (OOM at ~9 s) |

14-19 s per matrix run, measured 7 times (4 on a test port, 3 through the public URL).

## Tests

| Suite | Count | Command | Result |
|---|---|---|---|
| Unit | 49 | `python3 -m unittest tests/test_guard.py tests/test_frappe.py` | 49 pass, 7.7 s |
| End to end, against the running runtime | 20 cases (14 functions, 3 parametrized) | `./e2e.sh` | 20/20, 214 s |

The unit tests cover the failure cases: production DSN and port-80 test targets refused, malformed / missing / unreadable manifests, bridge policy (subselect, JOIN, `LOAD_FILE`, `GRANT`, `\!`), the exact `ssh` argv with a fake `ssh` on PATH (exit 2, timeout, fault copies never reach ssh), count / record / login mismatches, run API errors (400, 404, 409).

## Not measured

- A microVM sandbox (no nested KVM on Vultr vc2).
- Heal rounds 2-3 on the Frappe path live (unit-level only; on the Postgres and drill paths a planted lossy bridge was repaired in round 2).
- Large ERPs: the demo site has 135 doctypes and 1 Demo Service Request.
