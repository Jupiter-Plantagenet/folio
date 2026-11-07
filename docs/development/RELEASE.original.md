# Folio 0.2.0-local.1: local release runbook

Folio is a local invoice / purchase-order / receipt reconciliation workbench. It ships with a separate demonstration target ledger, **Northstar**. This runbook is for an operator on this Windows 11 host.

Every component binds to `127.0.0.1` only. This is a local release, not a public deployment. Nothing in this document authorizes publication, real data or payments.

Verdicts per acceptance ID are in `docs/parallel-delivery-round5-ops.md`. This file does **not** claim release readiness: `/health` reports `"release_ready": false`, and the model quality gate has not passed.

---

## 1. Source identity

| Item | Value |
|---|---|
| Version | `VERSION`: `0.2.0-local.1` |
| Source set | every file under `folio/` that `.gitignore` does not exclude, minus raw evidence (`evidence/`, `docs/round*-logs/`, `evidence-*.log`) |
| Source manifest | `evidence/documents/delivery/round5/source-manifest.sha256` (`sha256  path`, one line per file) |
| Manifest SHA256 | see `docs/parallel-delivery-round5-ops.md` §10 (regenerate with `scripts/round5_inventory.py`) |

The repository has **no commits**, so "tracked" means "not ignored". To verify a copy:

```bash
cd <copy>/folio && sha256sum -c <path>/source-manifest.sha256
```

Regenerate the manifest, licence inventory and secret scan with:

```bash
PYTHONPATH=. .venv/Scripts/python.exe -B scripts/round5_inventory.py
```

Pinned runtime inputs:

- **Python:** 3.12.10; `requirements.lock` now pins versions and hashes (SHA256 `2a83dfd6952bd80e0dcaca388ff5d9a7f0a2c3092af54b5bb264f2b6924d8243`). Fresh installs use `pip install --require-hashes -r requirements.lock`; clean-host reproduction remains open.
- **Node and npm:** Node 24.14.1 and npm 11.11.0, using `web/package-lock.json`.
- **Docker Engine:** 29.6.1.
- **Parser image:** `folio-parser:1` (image `sha256:1b67d3b2c11bca3f4ba93f1dda6a094ae57eeddbfbb9e7fdf87c85e62ab0d00b`), built from `ops/parser.Dockerfile`. Its base and Python requirements are pinned by digest/hash; the parser lock SHA256 is `501eb514b4a305bae0e60660bb93933a7535afc34c2db65dfaa76cc8e03ee07f`.
- **Model:** Qwen3-8B-Q4_K_M.gguf, from `Qwen/Qwen3-8B-GGUF` at revision `7c41481f…`. Its SHA256 is `d98cdcbd…5785` and its licence is Apache-2.0. It runs on llama.cpp CUDA build 10964 (`scripts/model_serve.py` verifies the weights hash before launch).

## 2. Components and ports

| Component | Port | Process |
|---|---|---|
| Northstar target | 8201 | `uvicorn folio.target:app` |
| Workbench API | 8101 | `uvicorn folio.workbench:app` |
| Compiled staging (UI and API, same origin) | 8111 | `serve_local.py --serve-child --ui-dir web/.cache/application-build-*` (validated candidate recorded in supervisor state) |
| Vite UI | 5101 | `node node_modules/vite/bin/vite.js` (proxies `/api` to 8101) |
| Local model | 18301 | `scripts/model_serve.py`, which launches `llama-server.exe` |
| Receipt/analysis/dispatch worker | — | `python -m folio.worker --loop` |
| Export worker | — | `python -m folio.exportworker --once`, re-run every 3 s by the supervisor |
| PostgreSQL | 5541 | Docker container `folio-postgres` (shared by every Folio database) |

Database principals come from the private file `.local/database-profiles.json`, profile `browser`. **Never print or commit it.**

Schema mode is always `runtime`. In that mode the services use non-owner, RLS-bound logins and **never** run DDL.

## 3. Start, stop and status

Run all commands from `C:/Users/user/portfolio-build/folio`.

```bash
# status: PIDs, alive flags, restart counts, HTTP status; no secrets
PYTHONPATH=. .venv/Scripts/python.exe scripts/live_supervisor.py status

# stop: kills ONLY the process trees the supervisor recorded (PID and create-time checked)
PYTHONPATH=. .venv/Scripts/python.exe scripts/live_supervisor.py stop
```

To start detached, so the stack survives the terminal or agent session:

```powershell
Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{CommandLine='"C:\Users\user\portfolio-build\folio\.venv\Scripts\python.exe" -B C:\Users\user\portfolio-build\folio\scripts\live_supervisor.py run'; CurrentDirectory='C:\Users\user\portfolio-build\folio'}
```

How the supervisor behaves:

- **Ports.** It refuses to start if 5101, 8101, 8111, 8201 or 18301 is occupied. It never kills unrelated processes.
- **Restarts (round 6).** It restarts a crashed component at most **5 times per 10 minutes**. When that window is full the component is marked `RESTART_CAP_REACHED` (`status` shows `escalation`; `supervisor.log` records it) and is retried as soon as the window allows. It is never left down permanently.
- **Never silent (round 6).** Every exit, loop error, failed state write (for example disk full) and restart is a JSON line in `.local/live-round4/supervisor.log`. State writes are guarded, so a full disk no longer kills the supervisor. It writes `heartbeat_utc` every 5 s; `status` shows `supervisor_heartbeat_age_s` and `supervisor_hung`.
- **Watchdog (round 6).** `live_supervisor.py watchdog-install` registers the scheduled task `FolioSupervisorWatchdog` (every minute, `pythonw`, no window). It restarts a dead or hung supervisor with `run --adopt`, which adopts still-running recorded components (PID, create time and port ownership checked) instead of refusing their ports. `stop` writes `supervisor.stopped`, so the watchdog never resurrects an operator stop; `run` clears it. Remove the task with `watchdog-uninstall`.
- **Model.** On an 8 GB GPU the model holds about 5.9 GB of VRAM. To free the GPU, stop the supervisor. Killing only the model process causes the supervisor to restart it.

State and logs are kept privately in `.local/live-round4/` (`state.json` and `*.log`).

### Pausing the model to free host RAM or VRAM

```powershell
# Run from the Folio directory. Use a fresh log filename on each invocation.
# The wrapper checks C: space, available RAM and Windows commit, takes the
# product_lock.py lock, and releases only its own lock when the command exits.
& .venv\Scripts\python.exe -B ..\tools\round5_locked_step.py `
  --product folio --purpose folio-model-control --cwd . `
  --log docs\round6-logs\model-pause-<unique-utc>.raw.log -- `
  .venv\Scripts\python.exe -B scripts\live_supervisor.py pause-model `
  --lock-owner folio-model-control
# To resume, use resume-model and a different, fresh log filename.
# For dead-owner recovery use ..\tools\product_lock.py recover.
# Never create, delete or edit the lock by hand.
```

- **Lock check.** Both commands refuse with `BUILD_LOCK_HOLDER_REQUIRED` unless the lock directory exists and records that owner.
- **Pause.** It writes `.local/live-round4/model.paused`, then kills **only** the model process tree the supervisor recorded. While the marker exists:
  - the run loop never starts or restarts the model;
  - `status` shows `"model_paused": true`;
  - API `/health` reports `"model":"paused"`;
  - agent analysis runs fail closed (`status: blocked`, `error: MODEL_PAUSED`, 0 target writes). Nothing falls back to another provider.
  - Manual review keeps working.
- **Resume.** It removes the marker. The loop restarts the model within about 1 s without using up the crash-restart budget. Loading takes tens of seconds, so poll `status` until the model shows `http: 200`.
- **Check first.** Pause only when the model is idle: `curl -s http://127.0.0.1:18301/slots` must show `"is_processing":false`, and no evaluation process may be running.
- **Supervisor version.** A supervisor started before this feature existed must be restarted (`stop`, then start) before these commands take effect.

Health checks:

```bash
curl -s http://127.0.0.1:8101/health   # {"status":"ready","pending_jobs":N,"oldest_job_age_s":..,"verifier_lag_s":..,"alarms":[..],"worker":{..},"model":..}
curl -s http://127.0.0.1:8201/health
curl -s http://127.0.0.1:8111/health
```

Round 6 `/health` fields (DB clock):

- `pending_jobs` (queued or leased), `retrying_jobs` (uncertain, under 5 tries), `unknown_outcome_jobs` (exhausted, human action required);
- `oldest_job_age_s`, measured from approval; `verifier_lag_s`, the age of the oldest sealed but not yet verified effect;
- `analysis_runs_pending`, `oldest_analysis_run_age_s`, `export_jobs_pending`, `oldest_export_job_age_s`;
- `worker`: heartbeat state, age and consecutive failures;
- `alarms`: `JOB_AGE_GT_120S`, `VERIFIER_LAG_GT_120S`, `EXPORT_JOB_AGE_GT_120S`, `UNKNOWN_OUTCOME_REQUIRES_ACTION`, `WORKER_HEARTBEAT_STALE`/`_MISSING`, `WORKER_DATABASE_UNAVAILABLE`.

During a database outage `/health` returns **503** `{"status":"unready","alarms":["DATABASE_UNAVAILABLE",...]}`. API requests that cannot get a pooled connection within 10 s return **503 `DATABASE_BUSY`** with `Retry-After: 1` (pool bound: `FOLIO_DB_POOL_MAX`, default 10 per process; `FOLIO_DB_POOL_TIMEOUT`, default 10 s).

## 4. Backup

The required backup is **paired and quiesced**: the workbench and target are dumped together while the stack is stopped. A dump of one database on its own does not make the pair consistent.

1. Drain the queue. On 8101, wait until `/health` shows `"pending_jobs": 0`, and check there are no `seal_pending`/`sealed` cases.
2. Stop the stack: `live_supervisor.py stop`.
3. Dump both retained databases plus globals, without role passwords:

   ```bash
   B=.local/backups/$(date -u +%Y%m%dT%H%M%SZ); mkdir -p $B
   for db in folio_browser_wb_20261026080908 folio_browser_target_20261026080908; do
     docker exec folio-postgres pg_dump -U postgres -Fc $db > $B/$db.dump; done
   docker exec folio-postgres pg_dumpall -U postgres --globals-only --no-role-passwords > $B/globals-no-passwords.sql
   (cd $B && sha256sum *.dump *.sql > SHA256SUMS)
   ```

4. Copy the export artifacts. These are separate from the database: the private export root named in `scripts/live_supervisor.py` (`EXPORT_ROOT`).
5. Restart the stack (§3).

There is an alternative that does steps 3–4 for both databases, encrypts them (AES-256-GCM, with keys in `.local/backup-keys/`), and restore-tests each one into an isolated database. It also requires the supervisor to be stopped:

```bash
PYTHONPATH=".;scripts" .venv/Scripts/python.exe scripts/restore_drill.py
```

**Limits.**

- No backup is scheduled. RPO ≤ 24 h holds only if an operator runs a backup at least daily.
- Backup expiry of 30 days or less is **not enforced**.
- Key custody is on this host only.

## 5. Restore

**Never restore over the live databases.** Restore into new databases, validate them, and only then point a profile at them.

1. **Verify the dumps.** Run `sha256sum -c SHA256SUMS` in the backup directory.
2. **Restore target first, then workbench, into new names.**

   ```bash
   docker exec folio-postgres psql -U postgres -c 'CREATE DATABASE "folio_restore_<tag>_target"'
   docker exec -i folio-postgres pg_restore -U postgres --exit-on-error -d folio_restore_<tag>_target < target.dump
   # repeat for the workbench dump
   ```

3. **Reset restored sessions.** Run `DELETE FROM sessions` on the workbench and `DELETE FROM target_sessions` on the target.
4. **Run the integrity and reconciliation checks.** Use the same checks as the drill (`scripts/round5_restore.py`):
   - FK orphans;
   - migration journal against the files;
   - original-bytes hashes;
   - receipt before/after digests;
   - workbench proposal against target operation receipt.

   Any verdict other than `consistent_applied` or `no_effect_expected` means the pair is **inconsistent**. In that case:
   - keep dispatch disabled: do not start `folio.worker` or the export worker against it;
   - if the target is newer, preserve the target's truth;
   - never re-dispatch an operation whose receipt is absent in the restore. Create a new reviewed and approved proposal instead.
5. **Rotate credentials if the restore replaces a compromised host.** Rotate:
   - the workload identities (`.local/*.json`: broker, dispatcher and verifier tokens, and the approval Ed25519 key);
   - the database login passwords.

   Restored roles keep their old grants. None of this rotation was exercised in round 5.
6. **Switch the live profile.** Only after reconciliation, edit `.local/database-profiles.json` (private) to point at the restored databases. Then start the stack.

Drill evidence: `evidence/documents/restore/round5-*/report.json`.

## 6. Migrations (forward-only)

- **Files.** `ops/migrations/002-workbench.sql`, `002-target.sql`, `003-exports.sql`, `004-export-actor-lock.sql`, (round 6, expand-only indexes) `005-read-path-indexes.sql`, and (round 6, bounded immutable N-1 export row-lock compatibility) `006-n1-export-row-lock-compat.sql`. They are journalled in `schema_versions` with an exact-bytes SHA256. There are **no down migrations**. Rollback means restoring the pre-migration backup (§5).
- **Operator apply.** Back up first. Apply as a trusted owner login, never the runtime login:

  ```bash
  FOLIO_MIGRATION_DSN=<owner DSN, never printed> PYTHONPATH=. .venv/Scripts/python.exe scripts/export_migrate.py --apply --runtime-role <runtime login>
  ```

  - It verifies every journalled checksum and refuses on mismatch (`MIGRATION_CHECKSUM_MISMATCH`).
  - It refuses a runtime principal (`TRUSTED_NONRUNTIME_MIGRATION_OWNER_REQUIRED`).
  - Since round 5, it **skips (never re-executes)** any version already journalled, including the committed 004. Only grants are re-asserted.
  - It installs version 006 when absent. This is required for the archived immutable N-1 exporter: that version uses `SELECT ... FOR SHARE OF sessions, users`, which requires UPDATE privilege on both locked relations. Version 006 grants UPDATE only on a dedicated inert `users` column and installs a trigger that rejects every attempted update of that column. It does not grant UPDATE on auth fields or widen the table grant. The 002–005 migration bytes remain unchanged. An isolated authentic N-1 rehearsal is recorded in `docs/parallel-delivery-round6-n1-compat-addendum.md`; retained-schema application remains a separate coordinated operation.
- **Runtime startup.** In `FOLIO_SCHEMA_MODE=runtime`, startup only checks for 2, 3 and 4 and the 004 helper. If they are missing, it exits with `SCHEMA_MIGRATION_REQUIRED`. 005 is optional (indexes only), so app N runs on a schema without it and the previous app runs on a schema with it.
- **005 on a retained workbench DB.** After a backup: `PYTHONPATH=. .venv/Scripts/python.exe scripts/round6_apply_005.py <workbench db> <owner role> --dry-run`, then without `--dry-run`. It verifies the 2/3/4 checksums, runs as the owner in one transaction and skips if 005 is already journalled. `export_migrate.py --apply` also installs 005 when absent.
- **N-1 export compatibility (006).** Use the operator CLI above on an isolated restored copy first. It checksums 002–006, preserves existing journal entries, installs 006 only when missing, and reasserts the narrow capability grant. Do not edit/replay applied 002–005 SQL. The isolated proof is not evidence that any retained database currently has version 006; check the exact journal before claiming N-1 compatibility.
- **Bootstrap mode.** `FOLIO_SCHEMA_MODE=bootstrap` is for disposable databases only. It **re-executes** the 003/004 DDL on every start. Never use it on a retained database.

## 7. Troubleshooting

| Symptom | Safe reads | Action | Never |
|---|---|---|---|
| Supervisor exits with `PORTS_OCCUPIED:<names>` | `netstat -ano` for the ports | Identify the owner. Stop it only if it is a recorded Folio tree. | Kill unrelated processes. |
| `/health` gives `"model":"unavailable"` | `.local/live-round4/model.log`; `nvidia-smi` | Manual review still works. Restart the stack. Check VRAM (about 5.9 GB needed). | Load a second model. |
| DB outage | `/health` 503 `DATABASE_UNAVAILABLE`; `worker.log` shows `database_unavailable` lines with backoff | Round 6: the worker stays up and retries with jittered backoff (1 to 30 s); the API replaces stale pooled connections. Nothing to do once the DB is back. | Restart components in a loop. |
| Job `uncertain` | Case, proposal and job rows; the target `GET /operations/{key}` receipt | The worker re-claims after the 30 s lease and takes the read-only recovery branch. | Change the operation key or manually edit target amounts. |
| Job `exhausted`, event `DOC-recovery-action-required` (`LAST_ATTEMPT_DIED_OR_UNPUBLISHED`) | The target receipt for that operation key | A human compares the target receipt with the proposal. If it was applied, record that. If not, create a new reviewed and approved proposal. | Re-queue the old job. |
| Job `uncertain` with result `retry_scheduled` / `PRIOR_GRANT_UNEXPIRED` | Proposal `dispatch_retries` | Round 6: a worker died after sealing and before PATCH. The job is rescheduled for when the old grant expires (60 s); the next lease renews the grant (prior JTI, higher fence) and retries the **same operation key**. | Change the operation key. |
| Job `blocked`, `blocked_not_applied` | `result.reason` | Round 6: automatic recovery after a crash before PATCH or a target outage no longer blocks. A block now means authority changed (`STALE_APPROVAL_OR_AUTHORITY`), the target refused definitively (`TARGET_REFUSED_412` etc.), or an operator ran the read-only `--recover`. Re-review and re-approve. | Force-write. |
| Startup gives `SCHEMA_MIGRATION_REQUIRED` | `SELECT version FROM schema_versions` | Back up, then use the operator apply in §6. | Switch to bootstrap mode on retained data. |
| Proposal gives `REQUESTER_LOCK_CAPABILITY_REQUIRED` | — | Run `scripts/requester_lock_install.py` for that runtime login (one-time operator action). | Grant users UPDATE to the runtime login. |
| Export stays `queued` | `exportworker.log` | Check that the export root exists and is absolute. The worker leases for 30 s, with at most 5 tries. | Edit `export_jobs` rows. |
| Download gives `ARTIFACT_UNAVAILABLE` (503) | The artifact file hash against `export_jobs.artifact_sha256` | The file is corrupt or missing, and the product refuses to serve it. Restore it from the artifact backup. | Recompute and overwrite the stored digest. |
| Intake rejected or slow | API log | The parser runs in a networkless container (`folio-parser:1`) with 2 slots and a 256 MiB cap. | Run the parser on the host. |

To verify an exported evidence bundle independently, write a pack with `scripts/round5_proof.py`, then run the external verifier:

```bash
.venv/Scripts/python.exe -I scripts/round5_external_verify.py verify <pack.json> [report.json]
```

## 8. Known limitations (round 5)

- **Model.** The local model fails the DOC-T-MODEL thresholds. Manual review is the supported path.
- **Reference proof gaps.** There is no reference-hardware load proof, no scheduled or encrypted-by-default backup, and no backup expiry.
- **Health and audit.** Job-age and verifier-lag alarms exist since round 6 (`/health`); nothing pages a human, so an operator or monitor must poll them. The event hash chain is absent.
- **Credentials.** Source-default dev DSN passwords appear in `folio/workbench.py`, `folio/target.py`, `scripts/runtime.py` and `scripts/browser_proof.py`. They belong to loopback-only dev roles and must be removed or rotated before any publication.

## 9. F1 retained backup, migration and UI cutover record (2026-11-01)

This addendum records the executed local F1 operation. It does not supersede the
release limitations above or claim the Folio release gate is complete.

### Backup and restore rehearsal

- Before quiescence, an encrypted safety backup was written to
  `.local/backups/round6-prestop-20261101T134804Z`; its key is kept privately
  under `.local/backup-keys/`. This sequential pre-stop copy is a safety capture,
  not the consistency-qualified pair.
- The supervisor was then quiesced and the paired workbench/target backup,
  encrypted globals-without-role-passwords and four export artifacts were
  captured under `.local/backups/20261101134921`. Its manifest records two
  database dumps, four artifacts and nine verified checksums. Keys remain
  private under `.local/backup-keys/20261101134921`.
- The isolated restore rehearsal created
  `folio_restore_target_20261101134921` and
  `folio_restore_wb_20261101134921`. Authenticated dump readback, table
  counts/hashes, session reset and tombstone/FK checks are recorded in
  `evidence/documents/restore/20261101134921/report.json`. This is an isolated
  rehearsal; it did not replace the live databases or measure the full
  reference RPO/RTO target.
- Raw backup records: `docs/round6-logs/20261101-f1-prestop-safety-backup.log`,
  `docs/round6-logs/20261101-f1-final-backup-packet.log`, and
  `docs/round6-logs/20261101-f1-quiesce.log`.

### Migration 005

- The scratch rehearsal first failed transactionally when the runtime owner
  lacked permission on the PostgreSQL-owned migration journal. That attempt and
  diagnosis are preserved in `docs/round6-logs/20261101-f1-005-scratch-apply.log`
  and `docs/round6-logs/20261101-f1-005-scratch-apply-diagnosis.md`.
- The bounded trusted-owner rehearsal then applied migration 005 successfully
  to scratch. The live workbench dry-run verified journal versions 2, 3 and 4;
  the live apply as the trusted `postgres` owner journalled version 5 and
  created the four expected read-path indexes. Its exact SQL SHA256 was
  `9ac766dae0467db4823bf663516c912c92bcd5ec294ada8dcd227e2625b74461`,
  matching the unchanged migration file. The target database was not migrated.
- Raw records: `docs/round6-logs/20261101-f1-005-scratch-apply-postgres.log`,
  `docs/round6-logs/20261101-f1-005-live-dry-run.log`, and
  `docs/round6-logs/20261101-f1-005-live-apply.log`.

### Corrected UI bundle and current verification status

- The first staged immutable bundle flattened Vite's `assets/` tree and returned
  404 for its JavaScript and CSS; it is retained unchanged. The corrected
  immutable bundle is
  `web/.cache/application-build-r4-20261101T135358Z`. Its recursive file-set
  and SHA comparison matched `web/dist` before activation. The served index is
  SHA256 `e901ab394e30561dd290050d21903745c1a9a0a9390f74eea5acc6ba894b9357`;
  referenced JavaScript is
  `9c82fa68b56147c78000ea3be6054b97e24876cae42142f0b0fd6d9401a68152`, and
  CSS is `a2b35db9936c5d2d0e4753b3d847af91b2f7606bfb31b01f7434d583633cba85`.
  The staging server returned HTTP 200 for the index and both referenced assets,
  each matching the immutable bundle's SHA256.
- After the supervised restart, read-only status showed target, API, staging,
  HMR UI and worker alive; API health was ready with zero pending jobs,
  analysis runs or export jobs. The local model pause marker remained present;
  no watchdog task was installed. The UI bundle was restarted on the staging
  server at port 8111.
- The first headless Playwright page-boot command produced no result after
  about 65 seconds and was interrupted; its cause remains unknown and the
  complete no-result record is
  `docs/round6-logs/20261101-f1-staging-page-boot-hang-erratum.log`. A separate,
  bounded Chrome CLI DOM smoke subsequently passed in 0.55 s: the compiled page
  rendered the `Sign in to Folio` heading into a nonempty React root with
  document title `Folio`. Its resource gate, exact asset responses, DOM SHA256
  and result are recorded in
  `docs/round6-logs/20261101-f1-staging-dom-smoke.log`.
- The supervisor identity drill then killed only the PID/creation-time-verified
  supervisor, leaving all five persistent component PID/creation-time pairs
  unchanged. A detached `run --adopt` process recorded those same five as
  adopted; post-checks showed health 200, zero pending/analysis/export jobs,
  unchanged model pause marker and the same immutable bundle. The raw locked
  execution is `docs/round6-logs/20261101-f1-supervisor-adopt.log`. No watchdog
  task was installed, and there was no operator-stop marker during the drill;
  it does not claim a scheduled watchdog's stop behavior was exercised.
- Do not treat the successful DOM smoke/adoption drill as human workflow,
  dimension journey or release readiness evidence. The real-PostgreSQL/API
  EUR/kg, JPY/m and KWD/box browser journey and model-quality work remain open.

### Follow-up compiled UI cutover (reported 2026-11-02; run UTC 2026-11-01)

- The current compiled bundle is `web/.cache/application-build-r4-20261101T153114Z`.
  Its recursive three-file set matched `web/dist`; manifest:
  `evidence/documents/deploy/20261102/20261101T153114Z-immutable-build.json`
  (SHA256 `158d42650b7447333dcb1cecb3a2d4d1b7479cc0461333c3b8f96654ffad6207`).
- Before the locked staging-only restart, two online sequential database dumps
  were encrypted with AES-256-GCM and authenticated on readback. This is an
  additional safety copy, not a quiesced paired backup or restore rehearsal.
  The encrypted target dump SHA256 is
  `765d7a52d0334f5a2d47029cc3b0ac29849430521bb3497062cc9072b641620b`; the
  encrypted workbench dump SHA256 is
  `b8a36fbd70a4ed7026a5f980098a4e3efc1b48644db3e6cfaa2cc377c3e263bc`. Dump
  keys remain private under `.local/backup-keys/`.
- Under the shared lock the verified supervisor PID 29904 was replaced by PID
  29308 with `run --adopt`. The target/API/staging/Vite UI/worker identities
  were first adopted unchanged; only staging was then restarted from the new
  bundle, becoming PID 32592. Target PID 26824, API PID 32160, UI PID 33252 and
  worker PID 30940 remained unchanged. API, target and staging returned 200;
  API pending/retrying/unknown jobs, analysis and export queues were zero, with
  no alarms. The model pause marker remained byte-identical. Staging downtime
  duration was not sampled.
- The new served index (`efbe25d13df627eedc706e3477d759afbf7704f79c074df9d17dd881da6d8955`),
  CSS (`a2b35db9936c5d2d0e4753b3d847af91b2f7606bfb31b01f7434d583633cba85`) and
  JavaScript (`5026a46eca04efa5825c90edc194d75b7802860b06cc89e9a5622e6dcfc1c7a9`)
  returned HTTP 200 and matched the immutable bundle byte-for-byte. Chrome
  156.0.8078.79 rendered title `Folio`, heading `Sign in to Folio` and a
  nonempty React root; raw DOM and profile remain private. Diagnostic report:
  `docs/round6-logs/20261102-ui-dom-diagnostic-20261101T153756Z.json`.
- The first cutover Chrome assertion attempt and two earlier preflight/import
  failures are preserved and diagnosed in `docs/round6-logs/`; attempt 3 did
  activate the bundle and create the safety backup before the generic DOM
  assertion failed. The later diagnostic is a separate bounded pass. This
  page-boot proof is not a complete 14-screen workflow, accessibility review,
  model-quality result or release-readiness claim.

## Current N-1 compatibility and coherent restore (2026-11-02)

Migration006 is now applied to the retained workbench. Its bytes and all002–005
journal checksums are preserved. The runtime's inert-column row-lock grant is
bounded by an enabled unconditional rejection trigger; actual joined legacy row
locking passed and attempted inert UPDATE returned42501. See
`evidence/documents/migrate/n1-006-live-runtime-probe-20261102.json` and
`docs/parallel-delivery-round6-secdata.md` for raw attempts and source review.

For retained006 operations use `scripts/round6_apply_006.py` through the shared
lock wrapper with product `folio`, purpose `retained-n1-compat-006` and
`PYTHONPATH=.`. It creates a fresh encrypted backup, checks current authority and
predecessor hashes, and skips already-applied006. Generic `export_migrate.py`
refuses retained database identities; it remains a fresh/disposable operator tool.

The paired restore drill now also encrypts and restores all bounded private
export files, including `.expired-private`, and verifies restored DB references
against actual bytes. Current proof: `evidence/documents/restore/20261101221300/report.json`
(two databases,4 files,2.875seconds). Quiesce the supervisor and every one-shot
worker while holding the shared operator lock; the drill checks the operator-stop
marker, worker absence and other DB clients. Expired export bytes are privately
archived, and legal holds are rechecked before archival. No backup/expiry schedule
is installed, and reference RPO/RTO, off-host custody and backup expiry remain open.

## Current UI and model-evaluation state (2026-11-02)

The current immutable UI bundle is
`web/.cache/application-build-r4-20261101T230249Z`, superseding the earlier
cutover above. The signed-in workspace label and uncertain-request replay
workflow have independent source review, eight focused tests and a clean
TypeScript/Vite build. After fresh encrypted backup and supervisor restart, all
three artifacts served on8111 matched their build pins. See
`docs/round6-logs/reviewed-ui-staging-served-artifacts-20261102.json`.

V3 failed its sealed quality test and is consumed. V4 started held-out inference
but was interrupted at the host commit limit; it is also consumed/tainted and
must not be retried. No complete v4 quality result exists. A new independently
authored v5 is unsealed and undergoing blind QA. Authored fixture success is not
a substitute for these independent quality gates. The native model is currently
operator-paused while Fieldwork uses the host.

The60-minute reference LOAD remains pending. Its driver now enforces the standing
6GiB available RAM/50GiB C: space/system commit<85% gates, rechecks them during
load, stores raw output only under product-private C: scratch, and returns a
nonzero status on resource abort. Syntax and an actual bounded counter probe
passed (`docs/round6-logs/reference-load-resource-guard-20261102.json`); no load
measurement is claimed by that probe.
## Retention automation source packet (2026-11-02)

Added a bounded hourly retention hook in the normal supervisor, private decision
archive-before-current-projection redaction, and an operator backup inventory
archive command with a 30-day threshold and explicit pinned-release-evidence
exemption. The hook is skipped in model-only mode and defers under the shared
product lock; health exposes last-success lag and failure/defer alarms. No live
activation, test run, task installation, paired backup capture or restore was
performed. See `docs/RETENTION-OPERATIONS.md` for exact coverage and unresolved
read-path gaps. Historical event/proposal copies and already issued export
snapshots remain immutable and can still contain decision text; this packet does
not close full 365-day ordinary-access retention. Daily coherent paired-backup
automation still requires a reviewed quiesce/drain design.

### Retention/backup source follow-up (2026-11-02)

This follow-up supersedes the earlier retention source-packet coverage
statements above.

The implementation now adds encrypted archive-backed projections on case list
and detail, event SSE/activity, and proposal-bearing idempotent replays. Event
and proposal database bytes remain immutable; export snapshots use an allowlist
that omits decision/proposal text. Expired exports and backup inventories honor
legal holds and pinned release evidence. The supervisor source now schedules
hourly retention and a daily paired-capture state machine with a maintenance
gate, worker drain, shared claim barrier, bounded deferral and observable lag or
failure alarms. The paired helper encrypts and authenticates both database
dumps and the ready-export artifact pack; `restore_drill.py` reuses that helper
and retains its isolated restore validation.

Focused source tests passed (5 tests); syntax compilation passed for the touched
Python modules. No live activation, database access, capture, restore, scheduler
installation, key operation, service restart, or business-record mutation was
performed. Windows ACL behavior and runtime quiescence remain unproven. A stale
maintenance marker after supervisor loss intentionally blocks API work pending
operator recovery. No RPO/RTO, hardware I/O, off-host key custody, or crypto
erasure claim is made. See `docs/RETENTION-OPERATIONS.md` and root source review
for remaining activation criteria.

### Independent source review corrections (2026-11-02)

The review's history-cursor delay was addressed with a bounded private retry
queue for due immutable event/proposal identities deferred by legal holds,
release pins, or unresolved-effect gates. Deferred records are retried before
the main cursor each retention tick; overflow is visible and alarms. The private
storage helper now validates existing path ancestors before creation, rejects
non-C: Windows paths without resolving them, caches ACL setup in a bounded
process-local cache, and uses inherited ACLs for new files. Existing files are
reset once per process.

Updated source-only checks passed: syntax compilation and 8 focused tests (1
symlink test skipped because host symlink creation was unavailable). Logs are
under unique `work/retention-review-fix-*attempt*.log` names. These changes do
not validate Windows DACL behavior or paired-capture runtime identity. The
available PostgreSQL access rehearsal covers API grants/RLS/receipts/revocations
only; paired-backup `pg_stat_activity` visibility and actual capture identity
remain open for root's separate probe. No service, database, backup, restore,
task, or retained schema was changed here.

### Independent delta review corrections (2026-11-02, source freeze 11:29 UTC)

Inventory and immutable archive reads now validate lexical C:-only ancestors before
leaf metadata/glob/read operations. The supervisor no longer has a shadowed local
`os` binding; it selects a verified `application-build-*` under `web/.cache` and
records the selected build in supervisor state. The target maintenance-gate import
is top-level. Owned-path Ruff, syntax compilation, and focused tests passed (8
passed, 1 symlink-capability skip). Exact attempt logs and superseding hashes are
recorded in `work/retention-review-fix-manifest-20261102.md`.

The separate read-only profile visibility probe is evidence for current role
visibility/state only. It does not prove a quiesced coherent paired capture or
restore. No live capture or activation occurred.

## Current operator record — 2026-11-02 04:28 UTC

Retained migration008 and the reviewed access/retention application are active.
Existing migration002–006 bytes, checksums and timestamps remain unchanged.
Runtime roles have read access and narrowly scoped definer capabilities for
account administration, with no generic protected-table DML. Current immutable
UI is `web/.cache/application-build-r5-access-r2-20261102T0317Z`; index, JS and CSS
served on8111 all match their build hashes. Source/operations details and raw
logs are in `docs/parallel-delivery-round6-runtime.md`, final dated section.

Coherent encrypted backup20261102T041818Z captured both retained databases and
four export files while components were stopped. The supervisor subsequently
completed automatic paired capture20261102T042335Z. Actual isolated restore
`evidence/documents/restore/20261102T042741Z/report.json` matched all table-content
hashes and the four artifact DB references/bytes; both restored databases had
read-only flags and were removed after verification. It does not establish
reference RPO/RTO, off-host custody or full release acceptance.

Manual supervisor kill/adopt preserved exact PID/birth identities of all five
application components. Operator stop followed by manual watchdog invocation
returned `operator_stopped_noop`; application restart then restored all four
HTTP endpoints, with the model intentionally paused. No scheduled task, Windows
service or Run key was created. No watchdog removal is needed for this packet.

Current runtime/backup/migration/operator hashes are in
`docs/round6-logs/activation-restore-source-pins-20261102T0435Z.json` (SHA256
`ad144682fe7dbe2121cac02c6eeabd834f670b0c3e2efa60e99ef1b04a83273d`).
This packet manifest supplements historical source manifests; it is not a
complete final release source manifest.

F2 evidence export uses a private source mirror and private raw outputs, strict
bounded text scanning, scan-once exact-byte publication, fixed public rejection
summaries and validated test selectors. The publication review closure is
`docs/independent-review-secdata-publication-closure-20261102.md`; source tests
passed13 with3 platform skips, plus one final disclosure regression. Skips are
not acceptance passes. Disposable proof DBs/roles were removed by exact identity.

Release remains open: 900-second authored model stability, unconsumed v5 quality
evaluation, solo60-minute reference load, remaining security/reproduction gates,
all20 original IDs and14 screen assertions, and final current-source packaging.
The v3 failed and v4 interrupted held-out sets remain consumed and are not reused.


### Current capacity rollout — 2026-11-02

The 20 MiB/multipart and bounded100,000-cell parser source is active with compiled build `web/.cache/application-build-intake-20261102T0703Z`. Eight actual isolated image capacity probes passed before backed promotion; four application endpoints and all served UI file hashes passed independent root checks. Model remains paused and release readiness is not asserted. The full PostgreSQL run recorded1114pass45skip3fail54passing subtests; three corrected hostile-budget tests passed separately and import-only changes were independently reviewed. Current-source evidence and limitations are in `docs/parallel-delivery-intake-capacity-20261102.md` and the appended parent tracking entry. Rollback images `folio-parser:rollback-capacity-20261102t070859298516z` (originalcbef) and `folio-parser:rollback-capacity-20261102t074152370199z` (prioraf741d) remain. No Folio scheduled task was installed.
