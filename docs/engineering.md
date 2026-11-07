# Folio engineering overview

Accounts-payable teams need to reconcile invoices against purchase orders and receipts, explain partial amounts, and prove that an approved correction reached the ledger.

The implementation separates document parsing, recommendation, human approval, dispatch, and independent verification. Every proposal binds its source version and the target revision. The worker uses durable jobs, fencing tokens, and idempotency keys to recover from uncertainty about a write.

## Source map

`folio/` contains the backend. `web/` contains the React interface. `tests/` contains the regression and failure tests. `ops/` contains database and parser infrastructure. `scripts/` contains fixture, isolated-test, and service entry points.

[The reviewer guide](../REVIEWER_GUIDE.md) maps the claims to concrete tests and expected outputs.

## Repository byte integrity

Git preserves the raw source, vendor, and fixture bytes used by the provenance checks. Shell scripts use LF line endings across platforms. Runtime state and dependency installations are ignored.
