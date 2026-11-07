# Folio portfolio acceptance register

Adopted 2026-11-04 by user direction (`../goal-tracking/USER-FOLIO-PORTFOLIO-BAR-20261104.md`).
This replaces the original enterprise gates in `../portfolio-engineering/ACCEPTANCE-REGISTER.csv`
for Folio release purposes. The original register is kept unchanged for history.

Rules:

- **PASS** means the check was executed against current source and met its condition.
- **MEASURED** rows pass when the measurement was actually taken and is published in the README, whatever the value.
  The original targets are listed for context, not as gates.
- **REVIEWED** means a code review, not an execution. It is reported as such, never as an executed pass.
- Failed runs are kept; nothing is relabelled.

| ID | Portfolio pass condition | Kind |
|---|---|---|
| DOC-T-FLOW | Browser journey through the main path: import → document → reconciliation → decision → approval (different user) → execution → verified record. State survives a page refresh. The Northstar 100-quantity correction changes exactly the expected fields. A hold decision makes zero target writes. | PASS |
| DOC-T-INTAKE | PDF, CSV and XLSX happy paths accepted with field provenance. Encrypted and unsupported files rejected without losing the other accepted files in the batch. Duplicate import links to the existing case. | PASS |
| DOC-T-AUTH | Automated tests: unauthenticated requests denied; viewer cannot mutate; reviewer cannot approve own proposal; workspace A cannot read workspace B on key routes; writes without CSRF rejected. | PASS |
| DOC-T-STALE | One test: a decision made against a stale source/target version is rejected without a target write. | PASS |
| DOC-T-ARITH | Exact case 100×48 → net 4800.00, tax 384.00, gross 5184.00, retained 1036.80, face 6220.80; randomized property test against an independent Decimal oracle; cross-currency arithmetic rejected. | PASS |
| DOC-T-PROVENANCE | Every extracted field quote round-trips exactly to the stored source text; UI shows the highlight on the source; human corrections are visibly distinct. | PASS |
| DOC-T-PROOF | One approved operation produces exactly one target receipt and one revision increment, including on replay with the same idempotency key; the verifier confirms it. | PASS |
| DOC-T-INJECTION | 15–20 representative malicious documents (prompt injection in text, cells, metadata) produce zero target effects and zero unauthorized tool calls. | PASS |
| DOC-T-PARSER | Parser runs in a container with no network and resource limits set; tests show a network attempt fails and an over-limit job is killed. | PASS |
| DOC-T-UX | Automated axe scan on all 14 screens: zero serious/critical violations; no horizontal overflow at 390 px. | PASS |
| DOC-T-STATES | Draft survives refresh; protected actions show pending until the server confirms; dialogs return focus to the invoking control. | PASS |
| DOC-T-MODEL | Real local-model run on about 30 authored tasks, 1 trial; outcome accuracy and field precision published. Original target: ≥90% outcomes, ≥99% precision. | MEASURED |
| DOC-T-COST | Each model request logs token usage (or explicit "unknown"); per-packet tool/model-call caps enforced by a test. | PASS |
| DOC-T-LOAD | 5-minute smoke against the running stack; read p95, write p95 and 5xx rate published. Original target: read p95 < 400 ms, write p95 < 800 ms. | MEASURED |
| DOC-T-RECOVERY | Test: worker killed mid-job resumes or escalates without a duplicate effect. | PASS |
| DOC-T-RESTORE | Backup script plus a restore into a fresh database with row counts/hashes matching. | PASS |
| DOC-T-MIGRATE | All migrations apply cleanly to an empty database. (N-1 compatibility dropped.) | PASS |
| DOC-T-REPRO | Lockfiles committed; README setup steps (compose, venv, npm, parser image) bring up a working stack. | PASS |
| DOC-T-DELIVERY | `pip-audit` and `npm audit` show no critical findings; dependency license list generated; secret scan clean on committed source. | PASS |
| DOC-T-RETENTION | Dropped as a gate. Existing 90-day original expiry is described in the README as implemented, with its unit test if present. | REVIEWED |

Screens: all 14 (`overview, inbox, import, document, reconciliation, decision, approvals, changes, execution,
recovery, records, evidence, activity, settings`) must render with real data for the right role and be shown in
the README with screenshots.

Results are recorded in `PORTFOLIO-STATUS.md`.
