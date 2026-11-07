"""Round 6 DOC-T-LOAD read-path fixes: queue summaries and expand-only indexes (005)."""

import os
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

LIST_SQL = (
    "SELECT (body - 'sources' - 'field_provenance') FROM cases WHERE workspace=%s "
    "AND (body ? 'sample_digest' OR body ? 'intake_digest') ORDER BY id"
)


def login(c, name):
    s = c.post("/api/v1/auth/login", json={"username": name, "password": f"folio-{name}-local"}).json()
    return {"Origin": "http://127.0.0.1:5101", "X-CSRF-Token": s["csrf"], "Idempotency-Key": str(uuid4())}


def test_queue_list_is_a_summary_and_detail_keeps_full_evidence():
    from folio.workbench import WORKSPACE, app, migrate

    migrate()
    c = TestClient(app)
    h = login(c, "reviewer")
    prefix = f"/api/v1/workspaces/{WORKSPACE}"
    r = c.post(prefix + "/intakes/sample", headers=h)
    assert r.status_code == 202, r.text
    listing = c.get(prefix + "/cases").json()
    assert listing["projection"] == "queue-summary-v1"
    # An earlier uploaded intake may already own the same bytes. Both intake
    # routes deliberately link that case instead of creating a duplicate.
    item = next(x for x in listing["items"] if x["id"] == r.json()["case_id"])
    assert item.get("sample_digest") or item.get("intake_digest")
    assert "sources" not in item and "field_provenance" not in item
    assert item["source_count"] >= 3
    for key in ("id", "invoice", "supplier", "label", "state", "invoice_face_amount", "version"):
        assert key in item
    detail = c.get(prefix + "/cases/" + item["id"]).json()
    assert len(detail["sources"]) == item["source_count"]
    assert detail["sources"][0]["pages"] and detail["field_provenance"]
    # Every summary field is the stored value (detail only adds/expands, never differs).
    for key, value in item.items():
        if key not in ("source_count", "proposal"):
            assert detail[key] == value, key


def test_migration_005_is_expand_only_journalled_and_used_by_the_planner():
    from folio.workbench import WORKSPACE, migrate

    migrate()
    migrate()  # bootstrap re-run is idempotent
    with psycopg.connect(os.environ["FOLIO_DSN"]) as db:
        assert db.execute("SELECT count(*) FROM schema_versions WHERE version=5").fetchone()[0] == 1
        names = {r[0] for r in db.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public'")}
        assert {"cases_queue_listing", "proposals_case_id", "jobs_proposal", "analysis_runs_case"} <= names
        db.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r[0] for r in db.execute("EXPLAIN " + LIST_SQL, (WORKSPACE,)))
        assert "cases_queue_listing" in plan, plan
        plan = "\n".join(
            r[0] for r in db.execute("EXPLAIN SELECT id FROM proposals WHERE case_id=%s", (str(uuid4()),))
        )
        assert "proposals_case_id" in plan, plan
