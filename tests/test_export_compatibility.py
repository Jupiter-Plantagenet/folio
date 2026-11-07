"""Opt-in real PostgreSQL rehearsals; never select a retained application profile."""

import ast
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.environ.get("FOLIO_EXPORT_MIGRATION_PROOF") != "1",
    reason="explicit isolated migration rehearsal only",
)
TABLES = [
    "users",
    "sessions",
    "cases",
    "proposals",
    "jobs",
    "commands",
    "originals",
    "analysis_runs",
    "events",
    "schema_versions",
]
WORKSPACE = "11111111-1111-4111-8111-111111111111"


def hardened_journal(raw):
    """Execute the actual hardener's journal expression, not a weaker fixture."""
    import scripts.harden_database as hardener

    tree = ast.parse(Path(hardener.__file__).read_text())
    assignment = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "journal" for t in n.targets)
    )
    namespace = {**vars(hardener), "raw": raw, "sha": hashlib.sha256(raw).hexdigest()}
    exec(
        compile(
            ast.Module(body=[assignment], type_ignores=[]), hardener.__file__, "exec"
        ),
        namespace,
    )
    return namespace["journal"]


def docker(*args, data=None):
    return subprocess.run(
        [
            "docker",
            "exec",
            *(["-i"] if data is not None else []),
            os.environ.get("FOLIO_POSTGRES_CONTAINER", "folio-postgres"),
            *args,
        ],
        input=data,
        capture_output=True,
        check=True,
    ).stdout


def fingerprints(db, tables=TABLES):
    result = {}
    for table in tables:
        if table == "users":
            # 006 adds an inert lock-ACL column; compare the pre-existing business
            # identity fields when checking that migration preserved user records.
            rows = db.execute(
                "SELECT row_to_json(t)::text FROM "
                "(SELECT username,salt,password_hash,roles,active FROM users) t "
                "ORDER BY row_to_json(t)::text"
            ).fetchall()
            result[table] = {
                "count": len(rows),
                "sha256": hashlib.sha256(
                    json.dumps(rows, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            continue
        rows = db.execute(
            sql.SQL(
                "SELECT row_to_json(t)::text FROM {} t ORDER BY row_to_json(t)::text"
            ).format(sql.Identifier(table))
        ).fetchall()
        result[table] = {
            "count": len(rows),
            "sha256": hashlib.sha256(
                json.dumps(rows, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    return result


def report(name, value):
    folder = Path(os.environ["FOLIO_EXPORT_EVIDENCE"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(value, indent=2, default=str))


@pytest.mark.parametrize("base_version", [2, 3])
def test_operator_cli_restored_genuine_hardened_v2_preserves_history_and_grants(
    base_version,
):
    from folio.database_security import admin, provision_runtime
    from scripts.isolated_database import create_profile

    stamp = "compat" + str(time.time_ns())
    names, env = create_profile(ROOT, stamp)
    source = names["workbench"]
    source_dsn = env["FOLIO_DSN"]
    raw = (ROOT / "ops/migrations/002-workbench.sql").read_bytes()
    cid, pid, jid, key, oid, aid = [str(uuid4()) for _ in range(6)]
    with psycopg.connect(source_dsn) as db:
        db.execute(raw.decode())
        db.execute(hardened_journal(raw))
        db.execute(
            "INSERT INTO users VALUES('retained','salt','hash','[\"reviewer\"]',true)"
        )
        db.execute(
            "INSERT INTO sessions VALUES('retained','retained','csrf',clock_timestamp()+interval '1 day')"
        )
        db.execute(
            "INSERT INTO cases VALUES(%s,%s,'{\"version\":7}')", (WORKSPACE, cid)
        )
        db.execute(
            "INSERT INTO proposals VALUES(%s,%s,%s,'{\"version\":4}')",
            (WORKSPACE, pid, cid),
        )
        db.execute(
            "INSERT INTO jobs(id,workspace,proposal) VALUES(%s,%s,%s)",
            (jid, WORKSPACE, pid),
        )
        db.execute(
            "INSERT INTO commands VALUES(%s,'retained','/replay',%s,%s,'{\"frozen\":true}')",
            (WORKSPACE, key, "a" * 64),
        )
        db.execute(
            "INSERT INTO originals(workspace,id,case_id,sha256,raw) VALUES(%s,%s,%s,%s,%s)",
            (WORKSPACE, oid, cid, "b" * 64, b"retained original"),
        )
        db.execute(
            "INSERT INTO analysis_runs(id,workspace,case_id,body) VALUES(%s,%s,%s,'{}')",
            (aid, WORKSPACE, cid),
        )
        db.execute(
            "INSERT INTO events(workspace,case_id,kind,actor,body) VALUES(%s,%s,'retained','retained','{}')",
            (WORKSPACE, cid),
        )
        owner = db.info.user
    # Actual hardened RLS and nonowner grants, unique source database/role only.
    provision_runtime(source, "workbench")
    if base_version == 3:
        from folio.schema_journal import install

        with psycopg.connect(source_dsn) as db:
            install(db, 3, ROOT / "ops/migrations/003-exports.sql")
            db.execute(
                "INSERT INTO public.export_jobs(id,workspace,case_id,actor,snapshot,snapshot_sha256,state) VALUES(%s,%s,%s,'retained','{}',%s,'tombstoned')",
                (str(uuid4()), WORKSPACE, cid, hashlib.sha256(b"{}").hexdigest()),
            )
    dump = docker("pg_dump", "-U", "postgres", "-Fc", source)
    private = ROOT / ".local/isolated" / stamp
    (private / "v2.dump").write_bytes(dump)
    restored = "folio_restore_export_" + uuid4().hex
    admin(
        sql.SQL("CREATE DATABASE {} OWNER {}")
        .format(sql.Identifier(restored), sql.Identifier(owner))
        .as_string()
    )
    docker("pg_restore", "-U", "postgres", "--exit-on-error", "-d", restored, data=dump)
    dsn = psycopg.conninfo.make_conninfo(source_dsn, dbname=restored)
    runtime_dsn = provision_runtime(restored, "workbench")
    role = psycopg.conninfo.conninfo_to_dict(runtime_dsn)["user"]
    with psycopg.connect(dsn) as db:
        proof_tables = TABLES + (["export_jobs"] if base_version == 3 else [])
        before = fingerprints(db, proof_tables)
        assert all(x["count"] > 0 for x in before.values())
        columns = db.execute(
            "SELECT column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_name='schema_versions' ORDER BY ordinal_position"
        ).fetchall()
        assert [(x[0], x[2]) for x in columns] == [
            ("version", "NO"),
            ("sha256", "NO"),
            ("installed_at", "NO"),
        ]
        assert columns[2][1:] == ("timestamp with time zone", "NO", "clock_timestamp()")
        old_journal = db.execute(
            "SELECT version,sha256,installed_at FROM schema_versions ORDER BY version"
        ).fetchall()
        assert old_journal[0][1] == hashlib.sha256(raw).hexdigest()
    cmd = [
        sys.executable,
        str(ROOT / "scripts/export_migrate.py"),
        "--apply",
        "--runtime-role",
        role,
    ]
    cli_env = {**os.environ, "PYTHONPATH": str(ROOT), "FOLIO_MIGRATION_DSN": dsn}
    failed_grant = subprocess.run(
        cmd[:-1] + ["folio_absent_" + uuid4().hex],
        env=cli_env,
        capture_output=True,
        text=True,
    )
    assert failed_grant.returncode != 0 and "does not exist" in failed_grant.stderr
    with psycopg.connect(dsn) as db:
        assert fingerprints(db, proof_tables) == before
        assert db.execute(
            "SELECT to_regprocedure('public.folio_export_lock_actor(text)')"
        ).fetchone() == (None,)
    first = subprocess.run(cmd, env=cli_env, capture_output=True, text=True)
    report(
        f"migration-v{base_version}-first-cli.json",
        {
            "exit_code": first.returncode,
            "stdout": first.stdout,
            "stderr": first.stderr,
            "before": before,
            "columns": columns,
            "restored": restored,
        },
    )
    assert first.returncode == 0, first.stderr
    with psycopg.connect(dsn) as db:
        journal = db.execute(
            "SELECT version,sha256,installed_at FROM schema_versions ORDER BY version"
        ).fetchall()
        assert journal[: len(old_journal)] == old_journal
        assert journal[1][0] == 3
        assert journal[-1][0] == 6
        by_version = {row[0]: row[1] for row in journal}
        assert by_version[4] == hashlib.sha256(
            (ROOT / "ops/migrations/004-export-actor-lock.sql").read_bytes()
        ).hexdigest()
        assert (
            journal[1][1]
            == hashlib.sha256(
                (ROOT / "ops/migrations/003-exports.sql").read_bytes()
            ).hexdigest()
        )
        assert by_version[6] == hashlib.sha256(
            (ROOT / "ops/migrations/006-n1-export-row-lock-compat.sql").read_bytes()
        ).hexdigest()
        assert fingerprints(
            db, [t for t in proof_tables if t != "schema_versions"]
        ) == {k: v for k, v in before.items() if k != "schema_versions"}
    second = subprocess.run(cmd, env=cli_env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    with psycopg.connect(dsn) as db:
        assert (
            db.execute(
                "SELECT version,sha256,installed_at FROM schema_versions ORDER BY version"
            ).fetchall()
            == journal
        )
        assert db.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='export_jobs'::regclass"
        ).fetchone() == (True, True)
        assert db.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner=%s",
            (owner,),
        ).fetchone() == (11,)
    for version, original_hash, installed_at in journal:
        with psycopg.connect(dsn) as db:
            db.execute(
                "UPDATE schema_versions SET sha256=%s WHERE version=%s",
                ("0" * 64, version),
            )
        mismatch = subprocess.run(cmd, env=cli_env, capture_output=True, text=True)
        assert (
            mismatch.returncode != 0
            and "MIGRATION_CHECKSUM_MISMATCH" in mismatch.stderr
        )
        with psycopg.connect(dsn) as db:
            assert db.execute(
                "SELECT sha256,installed_at FROM schema_versions WHERE version=%s",
                (version,),
            ).fetchone() == ("0" * 64, installed_at)
            db.execute(
                "UPDATE schema_versions SET sha256=%s WHERE version=%s",
                (original_hash, version),
            )
    with psycopg.connect(dsn) as db:
        after = fingerprints(db, [t for t in proof_tables if t != "schema_versions"])
        assert after == {k: v for k, v in before.items() if k != "schema_versions"}
    with psycopg.connect(runtime_dsn) as db:
        assert db.execute(
            "SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False, False, False, False)
        for column in [
            "state",
            "attempt",
            "fence",
            "tries",
            "lease_until",
            "artifact_ref",
            "artifact_sha256",
            "artifact_bytes",
            "deleted_at",
        ]:
            assert db.execute(
                "SELECT has_column_privilege(current_user,'export_jobs',%s,'UPDATE')",
                (column,),
            ).fetchone() == (True,)
        for column in ["snapshot", "snapshot_sha256", "actor", "case_id", "expires_at"]:
            assert db.execute(
                "SELECT has_column_privilege(current_user,'export_jobs',%s,'UPDATE')",
                (column,),
            ).fetchone() == (False,)
        assert db.execute(
            "SELECT has_table_privilege(current_user,'export_jobs','DELETE')"
        ).fetchone() == (False,)
        assert db.execute(
            "SELECT has_column_privilege(current_user,'sessions','expires','UPDATE')"
        ).fetchone() == (True,)
        assert db.execute("SELECT token FROM sessions FOR SHARE").fetchall() == [
            ("retained",)
        ]
        assert db.execute(
            "SELECT has_schema_privilege(current_user,'public','CREATE')"
        ).fetchone() == (False,)
        assert db.execute(
            "SELECT has_column_privilege(current_user,'users',"
            "'n1_export_lock_capability','UPDATE')"
        ).fetchone() == (True,)
        assert db.execute(
            "SELECT has_column_privilege(current_user,'users','active','UPDATE')"
        ).fetchone() == (False,)
        assert db.execute(
            "SELECT has_table_privilege(current_user,'users','UPDATE')"
        ).fetchone() == (False,)
        assert db.execute(
            "SELECT u.username,s.token FROM sessions s JOIN users u USING(username) "
            "WHERE s.token='retained' FOR SHARE OF s,u"
        ).fetchall() == [("retained", "retained")]
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege,
            match="N1_EXPORT_LOCK_CAPABILITY_IMMUTABLE",
        ):
            db.execute(
                "UPDATE users SET n1_export_lock_capability=true WHERE username='retained'"
            )
    report(
        f"restored-v{base_version}-cli-readback.json",
        {
            "source": source,
            "restored": restored,
            "dump_sha256": hashlib.sha256(dump).hexdigest(),
            "before": before,
            "after_original_nine": after,
            "old_journal": old_journal,
            "new_journal": journal,
            "applied_twice": True,
            "mismatch_failed_closed": True,
            "runtime_grants_checked": True,
            "grant_failure_rolled_back_metadata_and_ddl": True,
            "original_ten_tables_includes_journal": TABLES,
        },
    )


def test_actual_browser_helper_provisions_compatible_nonowner_runtime(monkeypatch):
    from scripts import isolated_browser as browser

    class ServingBoundary(Exception):
        pass

    captured = {}
    original = subprocess.Popen

    def before_serving(command, *args, **kwargs):
        if "uvicorn" in command:
            captured.update(kwargs["env"])
            raise ServingBoundary()
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", before_serving)
    monkeypatch.setattr(sys, "argv", ["isolated_browser.py"])
    with pytest.raises(ServingBoundary):
        browser.main()
    assert captured["FOLIO_SCHEMA_MODE"] == "runtime"
    assert Path(captured["FOLIO_EXPORT_ROOT"]).is_absolute()
    assert str(ROOT / ".local/isolated") in captured["FOLIO_EXPORT_ROOT"]
    with psycopg.connect(captured["FOLIO_DSN"]) as db:
        assert db.execute(
            "SELECT version FROM schema_versions ORDER BY version"
        ).fetchall() == [(2,), (3,), (4,), (5,), (8,)]
        assert db.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' "
            "AND column_name='n1_export_lock_capability'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT has_column_privilege(current_user,'sessions','expires','UPDATE'),has_column_privilege(current_user,'export_jobs','state','UPDATE'),has_column_privilege(current_user,'export_jobs','snapshot','UPDATE')"
        ).fetchone() == (True, True, False)
        assert db.execute(
            "SELECT tableowner<>current_user FROM pg_tables WHERE tablename='export_jobs'"
        ).fetchone() == (True,)
        assert db.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='export_jobs'::regclass"
        ).fetchone() == (True, True)
        journal = db.execute(
            "SELECT version,sha256,installed_at FROM schema_versions ORDER BY version"
        ).fetchall()
    command = [
        sys.executable,
        "-c",
        "from folio.workbench import migrate;from folio.target import migrate as tm;migrate();tm()",
    ]
    runtime = subprocess.run(
        command, cwd=ROOT, env=captured, capture_output=True, text=True
    )
    assert runtime.returncode == 0, runtime.stderr
    once = subprocess.run(
        [sys.executable, "-m", "folio.exportworker", "--once"],
        cwd=ROOT,
        env=captured,
        capture_output=True,
        text=True,
    )
    assert once.returncode == 0, once.stderr
    report(
        "browser-helper-startup.json",
        {
            "runtime_startup_exit": runtime.returncode,
            "worker_once_exit": once.returncode,
            "worker_once_output": once.stdout,
            "worker_behavior": "explicit once only; no automatic scheduling",
            "journal": journal,
            "export_root_absolute_private_location": True,
            "nonowner_rls_grants_checked": True,
            "network_services_started": False,
        },
    )
