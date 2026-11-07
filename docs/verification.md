# Publication-candidate verification

Executed on 7 November 2026 using the local review copy.

- Fresh installation of the hash-locked Python dependencies and `pip check` passed.
- Fresh `npm ci` and the TypeScript/Vite build passed.
- `python verify.py` passed three focused authority and model-budget checks.
- The disposable-database runner passed 50 tests across ledger-response loss and restart, arithmetic, and separate approval. Each test executed against the publication copy and fresh databases.
- The current npm advisory query reported zero findings.

## Evidence

[Command receipt](verification/review-checks.json) records each command and its exit status. The command logs are in the same directory. [The npm audit result](verification/npm-audit.json) records the advisory query at verification time.

## Scope

The installs used fresh local environments and dependency directories on the original host. The broader original project results are historical measurements, with their original datasets, private runtime state, platform assumptions, and waivers. The full public deployment and full integration campaigns remain separate from these focused publication checks.

The small commands are documented in [the reproduction guide](reproduction.md). The test-to-claim mapping is in [the reviewer guide](../REVIEWER_GUIDE.md).
