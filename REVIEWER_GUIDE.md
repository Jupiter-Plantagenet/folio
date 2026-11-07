# Folio reviewer guide

Use the following checks to inspect the engineering claims. Commands run from the repository root with the documented dependencies installed.

| Claim | Implementation | Test | Command | Expected observation |
|---|---|---|---|---|
| A dropped commit response and worker restart produce one ledger effect | [folio/worker.py](folio/worker.py) | [tests/test_proof.py](tests/test_proof.py) | `python scripts/isolated_tests.py tests/test_proof.py` | One target receipt and the expected revision change |
| The model can call only case-bound read tools | [folio/agent.py](folio/agent.py) | [tests/test_agent.py](tests/test_agent.py) | `python verify.py` | Three focused authority and budget checks pass |
| A second user must approve the exact proposal | [folio/workbench.py](folio/workbench.py) | [web/tests/journey.spec.ts](web/tests/journey.spec.ts) | `python scripts/isolated_browser.py tests/journey.spec.ts` | The authored packet reaches a verified Northstar record |

## Evidence boundaries

The documented invoice example bills 120 units while receipts support 100. The proposed gross amount is USD 5,184.00 and the retained balance is USD 1,036.80. Historical held-out model results were 40%, 30%, and 31% on three separately authored sets. Those outcomes expose an extraction generalization problem and are retained with their limitations.

[Current publication verification](docs/verification.md) records the checks executed for this copy. [Reproduction instructions](docs/reproduction.md) distinguish the small review path from the full application and research runs.
