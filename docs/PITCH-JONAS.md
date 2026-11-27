# Jonas's pitch, verbatim (WhatsApp 2026-11-27 11:05)

### Restorer — “Know your backup works before the outage does.”

*Pitch*

A disaster-recovery rehearsal service that turns a backup plus recovery objective into a verified restore report, with every restore attempted in disposable compute.

*Core workflow and verifiable output*

The user uploads a small backup fixture plus a recovery goal such as “restore this application database and prove that orders and users are intact”. The control plane asks Vultr Serverless Inference to create a restore plan and script. A fresh sandbox runs it. The first deterministic demo fixture should deliberately fail with a realistic error — for example, wrong file path, schema mismatch or missing import. The UI displays ⁠ stderr ⁠; the orchestrator feeds that error to the model; the model repairs the script; a second sandbox run succeeds.

The verifier then runs fixed, non-LLM checks: row counts, selected hashes, schema assertions and one application smoke test. The final artefact is a *Recovery Proof* containing command receipts, checksums, assertions and elapsed time.

*Why containment is essential*

Backups are data plus executable assumptions: restore hooks, archive contents, migrations and helper scripts are exactly the kind of material you do not want executing in the control plane. A broken restore can also loop, consume memory/disk or try to reach an external service. The product only becomes credible if “test your backup” does not mean “trust your backup on the machine running the tester”.

*Twenty-second containment moment*

Select a fixture labelled *“Compromised backup”. During restore, a bundled post-restore helper attempts an outbound request and then enters an infinite loop. The activity timeline flashes **NETWORK: DENIED, CPU reaches the configured cap, and at 3 seconds the orchestrator terminates the sandbox. Show ⁠ exit: timeout ⁠, then **sandbox destroyed*.

Beside it, keep a green “Control Plane /health — 200” pulse ticking throughout. Finish on a red recovery verdict: “Backup unsafe; production untouched.” This demonstrates real enforcement without risking the host.

*Vultr reference architecture*

Browser → public Next.js or simple React UI → FastAPI orchestrator on the mandatory Vultr VM. The orchestrator alone holds the key for ⁠ https://api.vultrinference.com/v1 ⁠; use the live-selected reasoning model for plan/repair/report. It writes the uploaded fixture into a fresh sandbox input volume, starts *Microsandbox if KVM works immediately; otherwise hardened Docker/gVisor*, receives only stdout/stderr/artifacts, runs deterministic verification, deletes the sandbox, then calls Serverless Inference once more to explain the evidence. No VSI key enters the sandbox. Vultr's microVM model is especially well suited to this because it separates the guest kernel from the host.

*Nine-hour MVP*

Use SQLite or a tiny local Postgres fixture, not real AWS/S3/RDS. Build two canned-but-genuine backup files: one recoverable and one hostile. Support exactly three verification checks. Spend polish on the run timeline, evidence cards, sandbox-policy badge and host heartbeat. Cut authentication, scheduled drills, real cloud-storage integrations, alerting and multi-database support.



make a demo of this, a site that spins up and shows this in the best way
