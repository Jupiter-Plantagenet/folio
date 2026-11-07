# Folio portfolio acceptance status

Graded against [ACCEPTANCE-PORTFOLIO.md](ACCEPTANCE-PORTFOLIO.md) on 2026-11-04.
Raw evidence lives under `evidence/` and `.local/` on the build machine (not in git).

Final full real-PostgreSQL suite on the final code with the 300 dpi parser image:
**1,319 passed, 45 skipped, 0 failed** (13m16s; `.local/portfolio/fullsuite-7.log`). Browser suite on the final code
(`scripts/browser_suite.py`, real model included): **14/14 passed**.

**PASS** = executed against current source and met. **MEASURED** = measured and
published below; the original target is shown for context. **REVIEWED** = code review only.

| ID | Result | What was executed | Evidence |
|---|---|---|---|
| DOC-T-FLOW | PASS | Browser journey import → approval by a second user → verified Northstar record, with refresh (`journey.spec.ts`, `states.spec.ts`, `real-dimensions.spec.ts`); Northstar 100-unit correction changes exactly the 5 proposed fields + revision; hold writes nothing (`tests/test_portfolio_flow.py`) | `evidence/documents/finish-browser/`, `.local/portfolio/targeted-1.log` |
| DOC-T-INTAKE | PASS | PDF/CSV/XLSX intake with provenance; duplicate links existing case; encrypted PDF (incl. empty user password) and encrypted XLSX rejected per file while the good file is kept, through the real parser container (`tests/test_encrypted_intake.py`, `test_intake.py`, `test_formats.py`) | `.local/portfolio/targeted-2.log` |
| DOC-T-AUTH | PASS | Route/role matrix, CSRF/origin/workspace/viewer/self-approval denials with no job or target effect (`test_auth_matrix.py`, `test_auth_denials.py`) | full suite |
| DOC-T-STALE | PASS | Target edited after proposal → approval refused before enqueue; stale decisions rejected (`test_target_stale.py`, `test_auth_denials.py`) | full suite |
| DOC-T-ARITH | PASS | 100×48 → 4800.00 / 384.00 / 5184.00, retained 1036.80 of 6220.80; 48 quantities incl. 40 seeded random checked against an independent rational oracle; cross-currency/unit decisions rejected (`test_arithmetic.py`, `test_currency_unit_decisions.py`) | `.local/portfolio/targeted-1.log` |
| DOC-T-PROVENANCE | PASS | Every span round-trips to the hashed source text (`test_provenance_hostile.py`); browser shows the selected source-word highlight and a visible "Human-corrected" badge and corrections list (`states.spec.ts`) | browser run 2026-11-04 |
| DOC-T-PROOF | PASS | One receipt and revision 7→8 even after commit-response loss and restart; replay precedes If-Match; verifier rejects a wrong bound (`test_proof.py`, `test_target.py`, `test_verifier.py`) | full suite |
| DOC-T-INJECTION | PASS | 78 independently authored attacks (v5 product-channel 60 + hidden-spreadsheet 18), real local model: **0 attack successes, 0 target effects, 0 disallowed tool calls, 0 URL-bearing arguments**. Caveat: 0 of 42 benign controls completed (same extraction-coverage limit as MODEL) | `.local/eval-v5-portfolio/folio-v5-pop-0{2,3}/heldout/injection-grade.json` |
| DOC-T-PARSER | PASS | Networkless, read-only, non-root container; network probe fails; decompression bomb killed at memory limit; CPU spin killed at wall limit; oversize rejected before container (`test_parser_hostile.py`, `test_sandbox.py`) on image `sha256:927b60db…` | `.local/portfolio/targeted-2.log` |
| DOC-T-UX | PASS | axe (WCAG 2.0/2.1/2.2 A+AA) on all 14 screens at 1440 px and 390 px: zero serious/critical; no horizontal overflow at 1440/1024/390 (`routes.spec.ts`) | `evidence/documents/ux/` |
| DOC-T-STATES | PASS | Unsent decision draft survives a page reload; protected save stays pending/disabled until the server answers; dialog returns focus to its opener (`states.spec.ts`) | browser run 2026-11-04 |
| DOC-T-MODEL | MEASURED | Fresh held-out sets, each measured once: v5 (150) 40%; v6 (50) 30%; v7 (100) 31%; 0 target writes in all. See "Model quality" below | `.local/eval-v5-portfolio/`, `.local/eval-v6/`, `.local/eval-v7/` |
| DOC-T-COST | PASS | Every model request logs prompt/completion/total tokens and provider id, or explicit `"unknown"`; unknown usage is charged against the 12k-token run budget; run budget blocks before tool results (`test_agent.py`). Measured max per packet: 4 model calls, 5 tool calls, 6,637 prompt tokens, 156 completion tokens | `folio-v5-pop-01/heldout/summary.json` |
| DOC-T-LOAD | MEASURED | 5-minute mixed run, 1,000 packets / 10,000 pages, 20 users, 5 live model loops: read p95 **250 ms** (target <400), write p95 **515 ms** (<800), enqueue p95 0.41 s (<2 s), OCR 10-page p95 2.6 s (<90 s), local-model packet p95 18.5 s (<600 s), 5xx **0.15%** (<1%), peak stack RSS 300 MiB; 85 target operations, 0 duplicates. 9 of 80 OCR imports did not complete (5× 503 parser back-pressure, 4 connection read errors) | `evidence/documents/load/portfolio-20261104005941/summary.json` |
| DOC-T-RECOVERY | PASS | Worker killed before fence and after seal recovers with exactly one effect; target outage retried without duplicate (`test_dispatch_recovery.py`, `test_precommit_recovery.py`) | full suite |
| DOC-T-RESTORE | PASS | Quiesced paired backup (AES-256-GCM) of the retained workspace restored into fresh databases in 9.2 s; every table's row count and content hash match; 0 unvalidated FKs; export artifacts byte-verified | `evidence/documents/restore/20261104T001652Z/report.json` |
| DOC-T-MIGRATE | PASS | All bootstrap migrations (002, 003, 004, 005, 008) applied to brand-new empty databases twice; journal checksums match files; second run changed nothing (`scripts/portfolio_migrate_from_empty.py`) | `evidence/documents/migrate/portfolio-20261104000722/report.json` |
| DOC-T-REPRO | PASS | Fresh pinned Linux containers (Python 3.12 slim, Postgres 16 initialized by `ops/init.sql` like compose, Node 26) from the committed source: `pip install --require-hashes` matched all 41 locked pins; full suite **1,195 passed, 0 failed** (57 platform skips, e.g. the Windows-only evidence publisher); `npm ci` + UI build passed. Earlier repro attempts found and fixed four standalone/cross-platform defects (missing init roles, out-of-repo import, Linux launcher crash, host-dependent fixture bytes). Same Docker host, fresh userland (not a separate machine) | `evidence/documents/repro/round6-20261104T024118Z/summary.json` |
| DOC-T-DELIVERY | PASS | OSV advisory scan of all 121 locked Python/npm packages: 0 findings; secret scan of every committed text file: only documented demo-account fixtures and deliberate redaction-test inputs; licence inventory `evidence/documents/delivery/round5/licenses.json` | `evidence/documents/delivery/round6/vulnerabilities-20261104T000000Z/report.json` |
| DOC-T-RETENTION | REVIEWED | 90-day original expiry with legal hold is implemented and unit-tested (`test_retention.py`); live schedule not exercised | — |

Screens: all 14 render with real data and are captured in [docs/screenshots](docs/screenshots).

## Model quality (DOC-T-MODEL)

Held-out set **v5**: 150 tasks written by a separate AI author who never saw the product,
sealed before any product exposure, verified (1,253 files) before use, 1 trial, local
Qwen3-8B Q4_K_M on an RTX 5060.

| Slice | Allowed outcome |
|---|---|
| missing information | 30/30 |
| conflict | 30/30 |
| ordinary | 0/30 |
| unseen layout | 0/30 |
| poor scan | 0/30 |
| **overall** | **60/150 = 40%** (95% CI 33–48%); original target ≥90% |

Zero target writes, zero disallowed tools, zero wrong partial recommendations.
Extraction: supported-field precision **100%** (379/379), field coverage 25%: the
deterministic extractor refused to guess on table layouts and label variants it did not
know, so the model correctly held or escalated. This result is final for v5; v5 has since
been used as development data.

### Extractor work, then a fresh held-out set (v6)

Using v5 as development data, the extractor learned header-row tables, printed column
rules, invoice-number label variants and receipt-table synonyms (commit `9075f5f`).
On v5 (now development data, not a held-out claim) field coverage rose from 25% to 78% at
100% precision. An independent code review found three wrong-value defects in that work;
all were fixed with regression tests before measurement.

The one-time measurement used **v6**: 50 tasks from a new author agent that saw only the
public packet schema (`goal-tracking/FOLIO-HELDOUT-V6-SEAL.md`), verified before use.

| Measure | v6 result |
|---|---|
| Supported-field precision | **95.5%** (171/179), 8 wrong values |
| Field coverage | 37% |
| Allowed outcome (1 trial) | **15/50 = 30%** (95% CI 18–42%): conflict 10/10, missing 5/10, ordinary 0/10, unseen layout 0/10, poor scan 0/10 |
| Target writes / disallowed tools | 0 / 0 |

The 8 wrong values were real errors, not grading artifacts: six invoice-issuer readings
picked the bill-to company or a heading, one OCR unit price was a line total, and one unit
was wrong in a conflict task. None reached the ledger. The binding limit on v6 is receipt
reading: no receipt quantity was accepted (19 receipt tables rejected as unsupported
headers, 25 for receipt identity rules), so no partial could be supported and the model
held or escalated every packet. v5 and v6 are both consumed; any further extractor work
needs another independently authored set to be measured honestly.

### Second improvement round, then a third fresh set (v7)

Using v5 and v6 as development data (commit `abf6499`): receipt layouts, refined line
binding (invoice line numbers are vendor-local), bill-to exclusion for the issuer,
explicit invoice-number labels, credit-note and deposit handling, 300 dpi OCR, and a
**confirm-or-refuse** rule: quantity, unit price, tax rate and invoice total are reported
only when quantity × price + tax (+ printed freight) equals the printed total exactly.
Five independent reviews found wrong-value paths during this work; each was fixed with a
regression test. On the development sets field precision stayed 100% and the v6 model
outcome rose 30% → 72% (11/11 partial recommendations correct) — development numbers,
not held-out claims.

The one-time measurement used **v7**: 100 tasks, 288 documents in 34 designs, from a third
author agent that saw only the public schema (`goal-tracking/FOLIO-HELDOUT-V7-SEAL.md`),
seal verified before and after.

| Measure | v7 result |
|---|---|
| Supported-field precision | 95.6% (369/386); 17 wrong (13 invoice issuer, 4 unit) |
| Field coverage | 36%; money fields confirmed on 8/100 invoices; receipt quantity on 3/89 |
| Allowed outcome (1 trial) | **31/100 = 31%** (95% CI 22–40%): conflict 20/20, missing 11/20, ordinary 0/20, unseen layout 0/20, poor scan 0/20 |
| Target writes / disallowed tools | 0 / 0 |

**Conclusion:** the held-out score did not improve (v6 30% → v7 31%). The rule-based
extractor learned the conventions of the v5/v6 authors and did not generalize to a third
author's designs: on v7's 60 process-able packets the receipt was unreadable in 58 and the
money fields could not be confirmed in 56, so the assistant escalated or held. Safety held
throughout (no wrong partial, no target write). Raising this score further needs a
different extraction approach rather than more rules.
