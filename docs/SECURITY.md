# Security model

A backup is untrusted input: it can be corrupt, built for another app version, or carry code (a known RCE vector when restored on a staging server). BackupAirlock treats every restore as a detonation and keeps production out of reach.

## Guarantees and where they are enforced

| Guarantee | How | Code |
|---|---|---|
| Production is never written | the production client refuses any non-GET; its token needs read access only | `restoreguard/frappe_target.py:135-138` |
| A misconfiguration cannot aim a restore at production | test target on port 80, equal to the source, or named `demo.local` = config refused at startup; SSH host must be the test site's | `restoreguard/frappe_target.py:99-110` |
| Stand-in app configs cannot name a database | unknown keys or values that look like a DSN/host refused | `restoreguard/guard.py:78-90` |
| The runtime can restore one test site and nothing else | forced-command SSH key on the ERP VM: site hardcoded, argument must be `latest` or a `YYYYMMDD_HHMMSS-demo_local-database.sql.gz` name, `flock`, no pty or forwarding; host key pinned (`StrictHostKeyChecking=yes`) | `restoreguard/frappe_target.py:256-271` |
| Bad backups never reach a test site | the sandbox restore and every check run first; any failure stops the cell before SSH | `restoreguard/frappe_target.py:411` (`_cell`) |
| Model output cannot do more than fix data | bridge SQL must be `UPDATE` of the one doctype table; SELECT, JOIN, subselects, `INTO OUTFILE`, `LOAD_FILE`, `GRANT`, client escapes refused; Postgres bridges using COPY/PROGRAM refused | `restoreguard/frappe_target.py:391-405`, `restoreguard/guard.py:526` |
| The model never decides a verdict | verdicts come from sandbox outcomes and SQL checks | `restoreguard/guard.py:17-19`, `:565` |

## Sandbox (every restore, every bridge, every app boot)

`--network none --read-only --cap-drop ALL --security-opt no-new-privileges`, `--memory` = `--memory-swap` (Postgres 384 MB + app 128 MB; MariaDB 512 MB; drill 512 MB), `--pids-limit` 64-128, `--cpus 1`, non-root uid, data on a size-capped tmpfs, backup mounted read-only, wall-clock timeout, container removed in every outcome and the removal checked. One sandbox at a time per VM (file lock). The app under test joins the sandbox's network namespace, so it reaches the restored database on loopback and nothing leaves.

Code: `restoreguard/guard.py:220-236`, `restoreguard/frappe_target.py:297-307`, `ui/sandbox.py:86-100`.

## Quarantine

Containment signals: network attempt, write to the read-only root, read of the honey file (drill image only, `/root/.env` decoy), OOM kill (cgroup `memory.events`), timeout. Any signal or static-scan finding = `quarantined: DO NOT RESTORE TO PROD`, with an incident record (`/api/backups/{id}/incident.json`: scan findings with line numbers, signals, stderr, exit). Code: `restoreguard/guard.py:53-58`, `:360`; `ui/sandbox.py:38`, `:70`.

Measured: the poisoned Frappe copy (`INTO OUTFILE` into `/etc/cron.d` plus a client shell escape, `restoreguard/faults.py:50`) quarantined in 8.5 s on `rootfs` + 2 static-scan findings; the poisoned drill dump (`.shell` honey read + curl exfil, crontab write, `rm /usr/bin/sqlite3`, recursive-CTE memory bomb) tripped all 4 signals, 0 bytes left the sandbox.

## Secrets

All secrets come from `.env` (gitignored), which `install.sh` moves into `/etc/` with mode 600. No secret is ever mounted into a sandbox. `GET /api/config` (the Overview's facts) shows policies and targets but no tokens, keys or paths. Lines of the VM's restore output naming a password, secret, token or API key are withheld from the live log (`restoreguard/guard.py:173`).

## Known gaps

- Docker isolation, not a microVM: Vultr vc2 has no nested KVM. The Docker daemon is root-equivalent and never exposed to a sandbox.
- The live site has no login (judges can press Run). Runs are serialised and go only into test copies; the runtime API itself binds 127.0.0.1 and the console proxies only reads, rerun and Run (`ui/app.py:920-928`).
- The ERP VM's copy of a backup is trusted to be the file production's Backups page served (the forced command runs nothing else); it is not re-checksummed there.
