# Folio

Evidence-based invoice reconciliation with bounded AI recommendations.

Accounts-payable teams need to reconcile invoices against purchase orders and receipts, explain partial amounts, and prove that an approved correction reached the ledger.

Built from scratch as a local research software engineering project. The implementation includes application code, tests, and documented experiments.

## Engineering scope

Python services, exact monetary arithmetic, provenance, tool authority, and recovery across two independently stored systems.

The implementation separates document parsing, recommendation, human approval, dispatch, and independent verification. Every proposal binds its source version and the target revision. The worker uses durable jobs, fencing tokens, and idempotency keys to recover from uncertainty about a write.

## Review the work

- [Reproduce the focused checks](docs/reproduction.md)
- [Trace claims to implementation and tests](REVIEWER_GUIDE.md)
- [Inspect current verification and remaining setup work](docs/verification.md)
- [Watch the one-minute application walkthrough](docs/demos/folio-walkthrough.mp4)
- [Read the recorded project results](PORTFOLIO-STATUS.md)

## Repository layout

`folio/` contains the backend. `web/` contains the React interface. `tests/` contains the regression and failure tests. `ops/` contains database and parser infrastructure. `scripts/` contains fixture, isolated-test, and service entry points.

## Recorded result

The documented invoice example bills 120 units while receipts support 100. The proposed gross amount is USD 5,184.00 and the retained balance is USD 1,036.80. Historical held-out model results were 40%, 30%, and 31% on three separately authored sets. Those outcomes expose an extraction generalization problem and are retained with their limitations.

The recorded experiment reports describe the original development runs. The publication candidate has its own verification record, linked above, with the commands executed against this copy.

## Start here

Follow [the reproduction guide](docs/reproduction.md) to install the pinned dependencies, run the focused tests, and build the web application. The guide identifies the extra infrastructure and private inputs needed for larger experiments.

Third-party source retains its original copyright and license notices. A repository-wide distribution license is pending the author's publication review.
