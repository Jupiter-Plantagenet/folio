import pytest


def test_case_and_intake_event_rollback_with_callers_transaction():
    from folio.workbench import connect, create_case, migrate

    migrate()
    case = None
    with pytest.raises(RuntimeError, match="abort intake"):
        with connect() as db:
            case = create_case(db=db)
            raise RuntimeError("abort intake")
    with connect() as db:
        assert (
            db.execute(
                "select count(*) from cases where id=%s", (case["id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "select count(*) from events where case_id=%s", (case["id"],)
            ).fetchone()[0]
            == 0
        )
