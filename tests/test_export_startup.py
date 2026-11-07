"""Physical PostgreSQL startup dependency regression; disposable principals only."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.getenv("FOLIO_EXPORT_PROOF") != "1", reason="isolated export proof only"
)


@pytest.mark.parametrize(
    "state",
    [
        "v2",
        "v3",
        "v4",
        "missing-helper",
        "no-execute",
        "missing-2",
        "missing-3",
        "missing-4",
    ],
)
def test_runtime_startup_requires_export_dependencies(state):
    from test_export_compatibility import hardened_journal

    from folio.database_security import provision_runtime
    from folio.schema_journal import install
    from scripts.export_migrate import apply
    from scripts.isolated_database import create_profile

    stamp = "startup" + str(time.time_ns())
    names, env = create_profile(ROOT, stamp)
    owner = env["FOLIO_DSN"]
    raw = (ROOT / "ops/migrations/002-workbench.sql").read_bytes()
    with psycopg.connect(owner) as db:
        db.execute(raw.decode())
        db.execute(hardened_journal(raw))
        db.execute(
            "INSERT INTO users VALUES('retained','salt','hash','[\"auditor\"]',true)"
        )
        db.execute(
            "INSERT INTO sessions VALUES('retained','retained','csrf',clock_timestamp()+interval '1 day')"
        )
        db.execute(
            "INSERT INTO cases VALUES('11111111-1111-4111-8111-111111111111',%s,'{\"version\":7}')",
            (str(uuid4()),),
        )
        if state != "v2":
            install(db, 3, ROOT / "ops/migrations/003-exports.sql")
        before = db.execute(
            "SELECT version,sha256,installed_at FROM schema_versions ORDER BY version"
        ).fetchall()
    runtime = provision_runtime(names["workbench"], "workbench")
    role = psycopg.conninfo.conninfo_to_dict(runtime)["user"]
    with psycopg.connect(owner) as db:
        if state not in ("v2", "v3"):
            apply(db, role)
        if state == "missing-helper":
            db.execute("DROP FUNCTION public.folio_export_lock_actor(text)")
        if state == "no-execute":
            from psycopg import sql

            db.execute(
                sql.SQL(
                    "REVOKE EXECUTE ON FUNCTION public.folio_export_lock_actor(text) FROM {}"
                ).format(sql.Identifier(role))
            )
        if state.startswith("missing-") and state[-1].isdigit():
            db.execute(
                "DELETE FROM schema_versions WHERE version=%s", (int(state[-1]),)
            )
        before = db.execute(
            "SELECT version,sha256,installed_at FROM schema_versions ORDER BY version"
        ).fetchall()
    with psycopg.connect(runtime) as db:
        assert db.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False, False)
        assert db.execute("SELECT count(*) FROM users").fetchone() == (1,)
        assert db.execute(
            "SELECT to_regprocedure('public.folio_export_lock_actor(text)') IS NOT NULL"
        ).fetchone() == (state not in ("v2", "v3", "missing-helper"),)
        assert db.execute(
            "SELECT has_column_privilege(current_user,'users','active','UPDATE')"
        ).fetchone() == (False,)
    startup = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio;from folio.workbench import app,lifespan\nasync def start():\n async with lifespan(app):\n  print('READY')\nasyncio.run(start())",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            **env,
            "FOLIO_DSN": runtime,
            "FOLIO_SCHEMA_MODE": "runtime",
            "PYTHONPATH": str(ROOT),
        },
        capture_output=True,
        text=True,
    )
    with psycopg.connect(owner) as db:
        assert (
            db.execute(
                "SELECT version,sha256,installed_at FROM schema_versions ORDER BY version"
            ).fetchall()
            == before
        )
    folder = Path(os.environ["FOLIO_EXPORT_EVIDENCE"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ("startup-" + state + ".json")).write_text(
        json.dumps(
            {
                "state": state,
                "database": names["workbench"],
                "exit": startup.returncode,
                "stdout": startup.stdout,
                "stderr": startup.stderr,
                "journal": before,
                "nonempty": True,
                "nonowner": True,
            },
            default=str,
            indent=2,
        )
    )
    if state == "v4":
        assert startup.returncode == 0, startup.stderr
        assert startup.stdout == "READY\n"
        return
    assert startup.returncode != 0, (
        "runtime incorrectly reached READY without export dependency"
    )
    assert "SCHEMA_MIGRATION_REQUIRED" in startup.stderr
    assert "READY" not in startup.stdout
