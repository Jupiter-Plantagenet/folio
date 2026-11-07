"""Real-PG controls: a flagged restored pair remains read-only to both workers."""

import os

import psycopg
import pytest
from psycopg import sql


def test_existing_worker_connections_refuse_claims_on_database_restore_flag():
    from folio import exportworker, worker
    from folio.database_security import admin
    from folio.workbench import migrate

    migrate()
    with psycopg.connect(os.environ["FOLIO_DSN"], autocommit=True) as db:
        name = db.info.dbname
        # Warm the worker connection before setting the database metadata flag.
        with worker.connect():
            pass
        admin(
            sql.SQL("ALTER DATABASE {} SET folio.restore_mode='read_only'")
            .format(sql.Identifier(name))
            .as_string()
        )
        try:
            for claim in (worker.claim, exportworker.claim):
                with pytest.raises(RuntimeError, match="^READ_ONLY_RESTORE_MODE$"):
                    claim()
        finally:
            admin(
                sql.SQL("ALTER DATABASE {} RESET folio.restore_mode")
                .format(sql.Identifier(name))
                .as_string()
            )
        with worker.connect():
            pass


def test_worker_claims_fail_closed_when_restore_metadata_cannot_be_verified(
    monkeypatch,
):
    from folio import exportworker, restore_mode, worker
    from folio.workbench import migrate

    migrate()

    def unavailable(_db):
        raise RuntimeError("simulated metadata read failure")

    monkeypatch.setattr(restore_mode, "database_read_only", unavailable)
    for claim in (worker.claim, exportworker.claim):
        with pytest.raises(RuntimeError, match="^RESTORE_MODE_UNVERIFIABLE$"):
            claim()


def test_process_restore_flag_blocks_worker_before_lease_mutation(monkeypatch):
    from folio import exportworker, worker
    from folio.workbench import migrate

    migrate()
    monkeypatch.setenv("FOLIO_READ_ONLY", "restore")
    for claim in (worker.claim, exportworker.claim):
        with pytest.raises(RuntimeError, match="^READ_ONLY_RESTORE_MODE$"):
            claim()
