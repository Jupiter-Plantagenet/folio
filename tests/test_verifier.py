import json
from pathlib import Path


def test_independent_verifier_rejects_wrong_bound_before_snapshot():
    from folio.verifier import check
    from folio.workbench import connect

    evidence = (
        Path(__file__).resolve().parents[1] / "evidence/documents/proof/schedule.json"
    )
    operation = json.loads(evidence.read_text())["operation"]
    with connect() as db:
        proposal = db.execute(
            "select body from proposals where body->>'operation_key'=%s", (operation,)
        ).fetchone()[0]
    assert check(proposal)["outcome"] == "matched"
    wrong = {**proposal, "before_snapshot_digest": "f" * 64}
    assert check(wrong)["outcome"] == "conflict"
    unavailable = check(proposal, target="http://127.0.0.1:8299")
    assert (
        unavailable["outcome"] == "unavailable" and unavailable["write_requests"] == 0
    )
