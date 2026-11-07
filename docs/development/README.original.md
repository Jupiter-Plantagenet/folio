# Folio

**Invoice reconciliation with an AI assistant that can't move money on its own.**

Folio reads invoices, purchase orders and goods receipts (PDF, scanned PDF, CSV, XLSX),
works out how much of an invoice is actually supported, and proposes an exact change to
the accounts-payable record. A local LLM can prepare that proposal, but a *different*
human must approve the exact change, and an independent verifier re-reads the target
system before anything is marked done.

Everything runs locally: FastAPI + PostgreSQL + React, a sandboxed document parser in
Docker, and a local Qwen3-8B model through llama.cpp. No cloud APIs, no data leaves the
machine.

![Review proposed changes](docs/screenshots/changes.png)

## What it demonstrates

- **Human-in-the-loop AI with hard boundaries.** The model gets four typed, read-only
  tools (`read_sources`, `lookup_payable`, `compute_match`, `recommend_outcome`). It has
  no write, payment, URL or shell tool. Its output is a recommendation; the server
  re-validates it, and approval is a separate person's action.
- **Exactly-once effects on an external system.** Approved changes go through a durable
  job queue with fencing tokens, idempotency keys and If-Match revisions. The target
  ("Northstar Ledger", a separate service with its own database) records one receipt per
  operation. Tests kill the worker mid-flight and lose commit responses on purpose, then
  check for exactly one effect.
- **Provenance for every number.** Each extracted value points to the exact characters
  (and, for PDFs, page geometry) it came from. Click a value, see its source.
  Human corrections are versioned and visibly marked.
- **Hostile-input safety.** Uploads are parsed in a non-root, networkless, read-only
  container with CPU, memory, PID and wall-clock limits. Encrypted files are refused,
  spreadsheet formulas are never evaluated, and decompression bombs are killed at the
  memory limit. 78 prompt-injection documents written by an
  independent author produced zero unauthorized effects.
- **Decimal-exact money.** Currency- and unit-aware arithmetic, checked against an
  independent rational-number oracle.

## Screens

Fourteen screens cover the full workflow: overview, inbox, import, document & extracted
fields, reconciliation, decision, approval queue, proposed changes, execution, recovery,
verified records, evidence package, activity/audit, and settings.

| | |
|---|---|
| ![Document](docs/screenshots/document.png) | ![Reconciliation](docs/screenshots/reconciliation.png) |
| ![Execution](docs/screenshots/execution.png) | ![Activity](docs/screenshots/activity.png) |

All 14 are in [docs/screenshots](docs/screenshots).

## How it works

```
 Browser (React, :5101)
     │  session cookie + CSRF, If-Match, Idempotency-Key
     ▼
 Workbench API (FastAPI, :8101) ──► PostgreSQL "workbench" (RLS, non-owner runtime role)
     │        │
     │        └─► Parser sandbox (docker run --network none, 512 MiB, 64 PIDs, 120 s)
     │
     ├─► Analysis worker ──► local LLM (llama.cpp, Qwen3-8B, :18301), typed read-only tools
     │
     └─► Dispatch worker ──fenced, signed grant──► Northstar Ledger (:8201) ──► PostgreSQL "target"
                                                         ▲
                       Independent verifier (read-only DB role) ─┘
```

1. **Import:** files are parsed in the sandbox; fields are extracted with exact
   source offsets; ambiguous or unreadable fields stay *missing*, never guessed.
2. **Reconcile:** invoice vs. PO vs. receipts. The supported quantity is the minimum
   the documents actually prove.
3. **Decide:** a reviewer (or the model, if delegated) proposes *partial*,
   *no change*, *hold* or *escalate*. Only *partial* produces a patch.
4. **Approve:** a second person approves the exact patch digest. Reviewers cannot
   approve their own proposals.
5. **Execute and verify:** a fenced job applies the patch once; the verifier reads the
   target independently and only then marks it verified.

## Results

Measured on 2026-11-04 against the acceptance register in
[ACCEPTANCE-PORTFOLIO.md](ACCEPTANCE-PORTFOLIO.md); full table with evidence in
[PORTFOLIO-STATUS.md](PORTFOLIO-STATUS.md).

- **Tests:** about 1,300 pytest cases against real PostgreSQL and the real parser container,
  plus Playwright browser journeys (including one driven by the real local model).
- **Accessibility:** zero serious/critical axe violations (WCAG 2.2 AA rules) on all 14
  screens at desktop and 390 px mobile width.
- **Load (5 min, 20 users, 1,000 packets, 5 live model loops):** read p95 250 ms,
  write p95 515 ms, 5xx 0.15%, 0 duplicate effects across 85 executed changes.
- **Prompt injection:** 0 of 78 independently authored attacks succeeded.
- **Backup/restore:** encrypted paired backup restored into fresh databases with every
  table's row count and content hash matching.
- **Model quality:** see below. It is the weakest area, and the numbers are reported as
  measured.

### Model quality, honestly

On 150 held-out tasks written by a separate author who never saw this code:

| | missing info | conflict | ordinary | unseen layout | poor scan | overall |
|---|---|---|---|---|---|---|
| allowed outcome | 30/30 | 30/30 | 0/30 | 0/30 | 0/30 | **40%** |

The system never made a wrong change (0 target writes, 0 wrong partial
recommendations, 100% precision on every field it did extract), but it only extracted
25% of fields on unfamiliar layouts, so it correctly held back instead of acting.

That set then became development data. Two rounds of extractor work (real-world table
layouts, receipts, label variants, 300 dpi OCR, and a rule that quantity, price, tax and
total are only reported when they add up exactly) were each measured once on a **fresh**
set from a new independent author:

| Held-out set | Tasks | Allowed outcomes | Field precision | Target writes |
|---|---|---|---|---|
| v5 (before) | 150 | 40% | 100% | 0 |
| v6 (after round 1) | 50 | 30% | 95.5% | 0 |
| v7 (after round 2) | 100 | 31% | 95.6% | 0 |

On their own development data the same changes lifted the outcome from 30% to 72%, but
on unseen authors' documents the score did not move: the rule-based extractor learned
specific document conventions rather than general ones, so on new designs it still
cannot read most receipts and refuses the money fields, and the assistant escalates.
It never made a wrong change. Generalizing further needs a different extraction
approach, not more rules. Details: [PORTFOLIO-STATUS.md](PORTFOLIO-STATUS.md).

## Run it

Requirements: Windows 11 or Linux, Docker, Python 3.12, Node 24+, an NVIDIA GPU with
8 GB for the optional local model.

```bash
# 1. Dependencies (hash-locked)
python -m venv .venv
.venv/Scripts/python -m pip install --require-hashes -r requirements.lock   # Linux: .venv/bin/python
npm --prefix web ci

# 2. Database (loopback port 5541) and parser image
export FOLIO_POSTGRES_PASSWORD=<choose one>
docker compose -f ops/compose.yml up -d
docker build -f ops/parser.Dockerfile -t folio-parser:1 .

# 3. Seed, provision identities, harden roles
.venv/Scripts/python scripts/fixtures.py
PYTHONPATH=. .venv/Scripts/python scripts/bootstrap.py
PYTHONPATH=. .venv/Scripts/python scripts/provision_identity.py
PYTHONPATH=. .venv/Scripts/python scripts/harden_database.py --profiles main

# 4. Serve: UI http://127.0.0.1:5101, API :8101, Northstar :8201
PYTHONPATH=. .venv/Scripts/python scripts/serve.py --profile main

# Optional: local model on :18301. Put Qwen3-8B-Q4_K_M.gguf (Apache-2.0, from
# Qwen/Qwen3-8B-GGUF) in .cache/models/qwen3-8b-gguf-7c41481f/; its SHA-256 is checked
# before launch. Point FOLIO_LLAMA_SERVER at a CUDA llama.cpp `llama-server` binary.
PYTHONPATH=. .venv/Scripts/python scripts/model_serve.py
```

Demo accounts on a fresh install (synthetic data only): `reviewer`, `approver`, `viewer`,
`auditor`, `admin`, each with password `folio-<name>-local`. Northstar's operator
password comes from `FOLIO_TARGET_OPERATOR_PASSWORD` or the private `.local/` store.

Walkthrough: sign in as **reviewer** → *Import documents* → *Import authored sample
packet* → *Compare documents* → *Resolve exception* (quantity 100) → save. Sign out,
sign in as **approver** → *Approval queue* → approve the exact change → watch it execute
and verify. Expected: net 4800.00, tax 384.00, gross 5184.00, retained balance 1036.80.

### Tests

```bash
PYTHONPATH=. .venv/Scripts/python scripts/isolated_tests.py         # full suite, throwaway DBs
PYTHONPATH=. .venv/Scripts/python scripts/browser_suite.py          # Playwright journeys (+ FOLIO_REAL_MODEL_TEST=1)
PYTHONPATH=. .venv/Scripts/python scripts/portfolio_migrate_from_empty.py
PYTHONPATH=. .venv/Scripts/python scripts/round5_load.py 5          # 5-minute load
bash scripts/round6_repro_linux.sh   # clean Linux containers: hash-locked install, full suite, UI build
```

Each runner creates its own databases, roles and ports and never touches a running
workspace.

## Project layout

```
folio/        backend: API (workbench.py), target ledger, workers, parser, extractor,
              money/units, provenance, verifier, backup/restore, retention
web/src/      React UI (14 screens), request transport with exact replay
web/tests/    Playwright journeys, accessibility and state tests
tests/        pytest: auth, arithmetic, proof/fences, recovery, intake, parser sandbox...
ops/          Docker Compose, parser Dockerfile, SQL migrations
eval/         model evaluation and prompt-injection harness
scripts/      isolated runners, load/restore/migration drills, supervisor
docs/         design notes, ADRs, screenshots
```

## Limitations

- Field extraction generalizes poorly to unfamiliar layouts, receipts especially, so on
  new document styles the assistant mostly escalates (see model quality).
- Single-host local deployment; not hardened for internet exposure, and identity is local
  accounts only.
- Retention (90-day original expiry, legal hold) is implemented and unit-tested, but its
  live schedule was not exercised in this release.
- Synthetic, author-created documents only; no real customer data has been processed.
