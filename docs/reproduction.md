# Reproduce Folio checks

## Toolchain

Use Python 3.12 and Node 26.7.0. The repository records these in `.python-version` and `.node-version`. Run commands from the repository root. PowerShell examples below use the Windows virtual-environment path. On Linux or macOS, use `.venv/bin/python`.

## Install the pinned dependencies

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install --require-hashes -r requirements.lock
npm --prefix web ci --ignore-scripts --no-audit --no-fund
```

## Run the focused review

```powershell
python verify.py
```

This checks Python dependency consistency, runs three model-authority and budget tests, and builds the React interface with TypeScript checking. The receipt and command logs are written under `.local/review-checks/`.

## Reproduce ledger recovery in disposable databases

This path needs Docker, a local PostgreSQL 16 container on port 5541, and the parser image. The isolated runner creates fresh roles, identities, and databases for its run.

On a new machine, set `FOLIO_POSTGRES_PASSWORD` to your own private value, then create the infrastructure.

```powershell
docker compose -f ops/compose.yml up -d
docker build -f ops/parser.Dockerfile -t folio-parser:1 .
$env:PYTHONPATH=(Get-Location).Path
.venv/Scripts/python -X utf8 scripts/fixtures.py
.venv/Scripts/python -X utf8 scripts/isolated_tests.py tests/test_proof.py tests/test_arithmetic.py tests/test_flow.py
```

The expected proof is one ledger receipt and a single revision change after commit-response loss and worker restart. The arithmetic test checks the proposed amounts against an independent rational-number calculation. The separate-approval test checks who can authorize the change.

A PostgreSQL container already using the name `folio-postgres` is treated as existing operator infrastructure. Use this setup on a dedicated development machine or disposable environment.

## Run the browser journey

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH=(Join-Path (Get-Location) '.cache/browsers')
node web/node_modules/@playwright/test/cli.js install chromium
.venv/Scripts/python -X utf8 scripts/isolated_browser.py tests/journey.spec.ts
```

The browser imports the authored invoice packet, proposes a partial amount, changes to a separate approver, and observes the independently verified target record.

## Research evaluation

The recorded held-out populations and local Qwen model bytes are separate research inputs. Their preparation and scope are described in `PORTFOLIO-STATUS.md` and the original development README. The small review path above exercises authority, dependencies, and build behavior. A fresh model-quality measurement needs a separately prepared evaluation population and the local model runtime.
