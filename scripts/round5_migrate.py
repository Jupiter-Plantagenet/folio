"""Round-5 migration drill on SCRATCH databases only (never a live/retained database).

A. from empty: bootstrap migrate twice -> journal 2,3,4 checksums, idempotency, 004 re-execution probe
B. from the round-4 backup snapshots (browser wb = v2,3,4; main wb = v2 only): operator
   export_migrate.py --apply twice; data-table fingerprints unchanged
C. 004 refusal paths: runtime principal cannot migrate; runtime startup never installs;
   tampered journal checksum refuses
D. crash midway: SIGKILL-equivalent the operator migration while it is blocked inside its
   transaction (after journal row 3, before export_jobs DDL); verify rollback; rerun
E. N/N-1: immutable archived pre-004 app against schema N, including export enqueue;
   app N against schema N-1. N-1 bytes are checked against source-pins before/after.
Output: evidence/documents/migrate/round5-<stamp>/report.json (no secrets).
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from isolated_database import create_profile  # noqa: E402

from folio.database_security import admin, provision_runtime  # noqa: E402

BACKUP = ROOT / ".local/backups/round4-20261030T143651Z"
N1 = (
    ROOT
    / ".local/quarantined-evidence-mirrors/evidence/documents/exports/compat-backend-final-retry-1793073998089789400/source-mirror"
)
PY = sys.executable
DATA_TABLES = [
    "users",
    "cases",
    "proposals",
    "jobs",
    "commands",
    "originals",
    "analysis_runs",
    "events",
]
FN = "public.folio_export_lock_actor(text)"
N1_PINS = ROOT / "evidence/documents/exports/compat-backend-final-retry-1793073998089789400/source-pins.json"


def base_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("FOLIO_")}


def journal(dsn):
    with psycopg.connect(dsn) as db:
        return [
            list(map(str, r))
            for r in db.execute(
                "SELECT version,sha256,installed_at FROM schema_versions ORDER BY version"
            )
        ]


def fn_xmin(dsn):
    with psycopg.connect(dsn) as db:
        r = db.execute(
            "SELECT xmin::text FROM pg_proc WHERE oid=to_regprocedure(%s)", (FN,)
        ).fetchone()
        return r[0] if r else None


def fingerprints(dsn, tables=DATA_TABLES):
    out = {}
    with psycopg.connect(dsn) as db:
        for t in tables:
            if not db.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0]:
                continue
            rows = db.execute(
                sql.SQL("SELECT row_to_json(t)::text FROM {} t ORDER BY 1").format(
                    sql.Identifier(t)
                )
            ).fetchall()
            out[t] = {
                "n": len(rows),
                "sha256": hashlib.sha256(
                    "\n".join(r[0] for r in rows).encode()
                ).hexdigest(),
            }
    return out


def verify_n1_source_pins():
    """Verify the quarantined N-1 source against its preserved source pin list."""
    pins = json.loads(N1_PINS.read_text())
    results = {}
    for relative, expected in pins.items():
        path = N1 / relative
        if not path.is_file():
            raise RuntimeError("N1_SOURCE_PIN_MISSING")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("N1_SOURCE_PIN_MISMATCH")
        results[relative] = actual
    canonical_manifest = "\n".join(f"{path} {results[path]}" for path in sorted(results))
    return {
        "source_pins_sha256": hashlib.sha256(N1_PINS.read_bytes()).hexdigest(),
        "pinned_file_count": len(results),
        "source_manifest_sha256": hashlib.sha256(canonical_manifest.encode()).hexdigest(),
        "verified": True,
    }


def files():
    return {
        v: hashlib.sha256((ROOT / "ops/migrations" / n).read_bytes()).hexdigest()
        for v, n in [
            (2, "002-workbench.sql"),
            (3, "003-exports.sql"),
            (4, "004-export-actor-lock.sql"),
            (5, "005-read-path-indexes.sql"),
            (6, "006-n1-export-row-lock-compat.sql"),
        ]
    }


def operator_apply(owner_dsn, role):
    env = {**base_env(), "PYTHONPATH": str(ROOT), "FOLIO_MIGRATION_DSN": owner_dsn}
    r = subprocess.run(
        [
            PY,
            str(ROOT / "scripts/export_migrate.py"),
            "--apply",
            "--runtime-role",
            role,
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return {
        "exit": r.returncode,
        "stdout": r.stdout.strip()[-200:],
        "stderr_tail": r.stderr.strip().splitlines()[-1:] if r.stderr else [],
    }


def run_n1_compatibility_only(stamp, out, private_out, n1_source_before):
    """Run the authentic archived-N-1 enqueue, processing and download path."""
    out.mkdir(parents=True, exist_ok=True)
    private_out.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "n1_compatibility_only",
        "n_minus_1_source": str(N1.relative_to(ROOT)),
        "n1_source_before": n1_source_before,
        "migration_files_sha256": files(),
        "immutable_migrations_002_to_005": None,
    }

    def save():
        (out / "report.json").write_text(json.dumps(report, indent=2, default=str))

    n1_process = None
    log_handle = None
    try:
        workbench_name, owner_dsn, _ = restore_snapshot(
            "folio_workbench.dump", stamp, "n1compat"
        )
        runtime_dsn = provision_runtime(workbench_name, "workbench")
        role = runtime_role(runtime_dsn)
        report["scratch_database"] = workbench_name
        report["runtime_role"] = role
        first = operator_apply(owner_dsn, role)
        second = operator_apply(owner_dsn, role)
        report["operator_apply"] = {
            "first_exit": first["exit"],
            "second_exit": second["exit"],
            "idempotent": first["exit"] == 0 and second["exit"] == 0,
        }
        if first["exit"] or second["exit"]:
            save()
            raise RuntimeError("N1_COMPAT_OPERATOR_MIGRATION_FAILED")

        with psycopg.connect(owner_dsn) as db:
            journal = db.execute(
                "SELECT version,sha256 FROM schema_versions ORDER BY version"
            ).fetchall()
            auth_columns = db.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='users' "
                "AND column_name='n1_export_lock_capability'"
            ).fetchone()[0]
            case = db.execute(
                "SELECT id::text,body->>'version' FROM cases "
                "WHERE workspace=%s ORDER BY id LIMIT 1",
                ("11111111-1111-4111-8111-111111111111",),
            ).fetchone()
        expected_journal = [2, 3, 4, 5, 6]
        report["schema_journal"] = [int(row[0]) for row in journal]
        report["version6_checksum_matches_file"] = bool(
            journal
            and journal[-1][0] == 6
            and journal[-1][1]
            == hashlib.sha256(
                (ROOT / "ops/migrations/006-n1-export-row-lock-compat.sql").read_bytes()
            ).hexdigest()
        )
        if [row[0] for row in journal] != expected_journal or auth_columns != 1:
            save()
            raise RuntimeError("N1_COMPAT_SCHEMA_READBACK_FAILED")
        if not case or not case[1]:
            save()
            raise RuntimeError("N1_COMPAT_CASE_FIXTURE_MISSING")

        with psycopg.connect(runtime_dsn) as db:
            report["runtime_privileges"] = {
                "dedicated_lock_column_update": db.execute(
                    "SELECT has_column_privilege(current_user,'users',"
                    "'n1_export_lock_capability','UPDATE')"
                ).fetchone()[0],
                "auth_active_update": db.execute(
                    "SELECT has_column_privilege(current_user,'users','active','UPDATE')"
                ).fetchone()[0],
                "users_table_update": db.execute(
                    "SELECT has_table_privilege(current_user,'users','UPDATE')"
                ).fetchone()[0],
                "legacy_joined_row_lock": db.execute(
                    "SELECT u.username,s.token FROM sessions s JOIN users u USING(username) "
                    "WHERE s.token=(SELECT token FROM sessions WHERE username='reviewer' "
                    "ORDER BY expires DESC LIMIT 1) FOR SHARE OF s,u"
                ).fetchone()
                is not None,
            }
        try:
            with psycopg.connect(runtime_dsn) as db:
                db.execute(
                    "UPDATE users SET n1_export_lock_capability=true "
                    "WHERE username='reviewer'"
                )
        except psycopg.errors.InsufficientPrivilege as exc:
            report["runtime_privileges"]["capability_dml_rejected"] = (
                exc.sqlstate == "42501"
                and "N1_EXPORT_LOCK_CAPABILITY_IMMUTABLE" in str(exc)
            )
        if not report["runtime_privileges"]["dedicated_lock_column_update"]:
            save()
            raise RuntimeError("N1_COMPAT_LOCK_GRANT_MISSING")
        if (
            report["runtime_privileges"]["auth_active_update"]
            or report["runtime_privileges"]["users_table_update"]
            or not report["runtime_privileges"]["legacy_joined_row_lock"]
            or not report["runtime_privileges"].get("capability_dml_rejected")
        ):
            save()
            raise RuntimeError("N1_COMPAT_PRIVILEGE_BOUNDARY_FAILED")

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        api_env = {
            **base_env(),
            "FOLIO_DSN": runtime_dsn,
            "FOLIO_SCHEMA_MODE": "runtime",
            "PYTHONPATH": str(N1),
            "FOLIO_EXPORT_ROOT": str((private_out / "exports").resolve()),
            "FOLIO_TARGET_URL": "http://127.0.0.1:9291",
        }
        api_log = private_out / "n1-api.log"
        log_handle = api_log.open("w")
        n1_process = subprocess.Popen(
            [
                PY,
                "-B",
                "-m",
                "uvicorn",
                "folio.workbench:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=N1,
            env=api_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
        healthy = False
        for _ in range(60):
            if n1_process.poll() is not None:
                break
            try:
                health = client.get("/health")
                if health.status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        report["n1_health_200"] = healthy
        if not healthy:
            save()
            raise RuntimeError("N1_COMPAT_API_NOT_HEALTHY")

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "reviewer", "password": "folio-reviewer-local"},
            headers={"Origin": "http://127.0.0.1:5101"},
        )
        report["n1_login_status"] = login.status_code
        if login.status_code != 200:
            save()
            raise RuntimeError("N1_COMPAT_LOGIN_FAILED")
        csrf = client.get("/api/v1/auth/session").json()["csrf"]
        case_id, case_version = case
        enqueue = client.post(
            "/api/v1/workspaces/11111111-1111-4111-8111-111111111111/"
            f"cases/{case_id}/exports",
            json={
                "case_version": int(case_version),
                "include_originals": False,
                "redaction_profile": "audit-v1",
            },
            headers={
                "Origin": "http://127.0.0.1:5101",
                "x-csrf-token": csrf,
                "idempotency-key": str(uuid4()),
            },
        )
        report["n1_export_enqueue_status"] = enqueue.status_code
        if enqueue.status_code != 202:
            report["n1_export_enqueue_response"] = enqueue.text[:240]
            save()
            raise RuntimeError("N1_EXPORT_ENQUEUE_COMPATIBILITY_FAILED")
        job_id = enqueue.json()["artifact_job_id"]
        report["artifact_job_id"] = job_id

        worker_env = {**api_env, "PYTHONPATH": str(ROOT)}
        worker_log = private_out / "export-worker.log"
        worker = subprocess.run(
            [PY, "-B", "-m", "folio.exportworker", "--once", "--job", job_id],
            cwd=ROOT,
            env=worker_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        worker_log.write_text(worker.stdout + worker.stderr)
        report["export_worker"] = {
            "exit": worker.returncode,
            "stdout": worker.stdout.strip(),
        }
        if worker.returncode:
            save()
            raise RuntimeError("N1_COMPAT_EXPORT_WORKER_FAILED")

        with psycopg.connect(runtime_dsn) as db:
            artifact_row = db.execute(
                "SELECT state,artifact_ref,artifact_sha256,artifact_bytes "
                "FROM export_jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        if not artifact_row or artifact_row[0] != "ready":
            save()
            raise RuntimeError("N1_COMPAT_ARTIFACT_NOT_READY")
        artifact_path = private_out / "exports" / artifact_row[1]
        artifact_bytes = artifact_path.read_bytes()
        artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
        downloaded = client.get(
            f"/api/v1/workspaces/11111111-1111-4111-8111-111111111111/"
            f"artifacts/{job_id}?download=true"
        )
        report["n1_artifact_download_status"] = downloaded.status_code
        report["artifact"] = {
            "state": artifact_row[0],
            "sha256_db": artifact_row[2],
            "bytes_db": artifact_row[3],
            "sha256_file": artifact_digest,
            "bytes_file": len(artifact_bytes),
            "download_sha256": hashlib.sha256(downloaded.content).hexdigest(),
            "download_matches_file": downloaded.content == artifact_bytes,
            "integrity_matches_db": artifact_digest == artifact_row[2]
            and len(artifact_bytes) == artifact_row[3],
        }
        report["n1_source_after"] = verify_n1_source_pins()
        report["n1_source_unchanged"] = (
            report["n1_source_before"] == report["n1_source_after"]
        )
        save()
        if downloaded.status_code != 200:
            raise RuntimeError("N1_COMPAT_ARTIFACT_DOWNLOAD_FAILED")
        if not report["artifact"]["download_matches_file"]:
            raise RuntimeError("N1_COMPAT_ARTIFACT_DOWNLOAD_MISMATCH")
        if not report["artifact"]["integrity_matches_db"]:
            raise RuntimeError("N1_COMPAT_ARTIFACT_INTEGRITY_FAILED")
        if not report["n1_source_unchanged"]:
            raise RuntimeError("N1_SOURCE_CHANGED_DURING_DRILL")
        applied_hashes_after = files()
        report["immutable_migrations_002_to_005"] = {
            "unchanged": all(
                report["migration_files_sha256"][v] == applied_hashes_after[v]
                for v in (2, 3, 4, 5)
            ),
            "after": applied_hashes_after,
        }
        save()
    finally:
        if n1_process is not None and n1_process.poll() is None:
            psutil.Process(n1_process.pid).kill()
            n1_process.wait(timeout=10)
        if log_handle is not None:
            log_handle.close()
        report.setdefault("n1_source_after", verify_n1_source_pins())
        report["n1_source_unchanged"] = report["n1_source_before"] == report["n1_source_after"]
        save()


def restore_snapshot(dump_name, stamp, tag):
    names, env = create_profile(ROOT, f"{tag}{stamp}")
    owner = psycopg.conninfo.conninfo_to_dict(env["FOLIO_DSN"])["user"]
    db = names["workbench"]
    r = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "folio-postgres",
            "pg_restore",
            "-U",
            "postgres",
            "--exit-on-error",
            "--no-owner",
            f"--role={owner}",
            "-d",
            db,
        ],
        input=(BACKUP / dump_name).read_bytes(),
        capture_output=True,
    )
    if r.returncode:
        raise RuntimeError("RESTORE_FAILED:" + r.stderr.decode(errors="replace")[:300])
    return db, env["FOLIO_DSN"], owner


def runtime_role(dsn):
    return psycopg.conninfo.conninfo_to_dict(dsn)["user"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compatibility-only",
        action="store_true",
        help="run only the authentic archived N-1 enqueue/worker/download rehearsal",
    )
    args = parser.parse_args()
    n1_source_before = verify_n1_source_pins()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    out = ROOT / "evidence/documents/migrate" / (
        f"n1-compat-{stamp}" if args.compatibility_only else f"round5-{stamp}"
    )
    out.mkdir(parents=True)
    private_out = ROOT / ".local/round5" / f"mig{stamp}"
    private_out.mkdir(parents=True, exist_ok=True)
    if args.compatibility_only:
        run_n1_compatibility_only(stamp, out, private_out, n1_source_before)
        print(json.dumps({"report": str(out / "report.json")}))
        return
    report = {
        "migration_files_sha256": files(),
        "down_migrations_present": sorted(
            p.name
            for p in (ROOT / "ops/migrations").iterdir()
            if "down" in p.name.lower() or "rollback" in p.name.lower()
        ),
    }

    def save():
        (out / "report.json").write_text(json.dumps(report, indent=2, default=str))

    # ---- A. from empty --------------------------------------------------------------
    names, owner_env = create_profile(ROOT, f"mge{stamp}")
    env = {
        **base_env(),
        **owner_env,
        "FOLIO_SCHEMA_MODE": "bootstrap",
        "PYTHONPATH": str(ROOT),
    }
    boot = [
        PY,
        "-c",
        "from folio.workbench import migrate;from folio.target import migrate as tm;migrate();tm()",
    ]
    r1 = subprocess.run(boot, env=env, cwd=ROOT, capture_output=True, text=True)
    j1, x1, f1 = (
        journal(owner_env["FOLIO_DSN"]),
        fn_xmin(owner_env["FOLIO_DSN"]),
        fingerprints(owner_env["FOLIO_DSN"]),
    )
    time.sleep(1)
    r2 = subprocess.run(boot, env=env, cwd=ROOT, capture_output=True, text=True)
    j2, x2, f2 = (
        journal(owner_env["FOLIO_DSN"]),
        fn_xmin(owner_env["FOLIO_DSN"]),
        fingerprints(owner_env["FOLIO_DSN"]),
    )
    expected = files()
    report["A_from_empty"] = {
        "database": names["workbench"],
        "first_exit": r1.returncode,
        "second_exit": r2.returncode,
        "journal_versions": [int(v) for v, _, _ in j1],
        "journal_matches_files": all(expected[int(v)] == s for v, s, _ in j1),
        "journal_identical_after_rerun": j1 == j2,
        "data_identical_after_rerun": f1 == f2,
        "fn_004_xmin_first": x1,
        "fn_004_xmin_after_rerun": x2,
        "fn_004_ddl_reexecuted_on_rerun": x1 != x2,
    }
    save()
    # runtime principal on schema N (used by E)
    for kind, database in names.items():
        raw = (ROOT / "ops/migrations" / f"002-{kind}.sql").read_bytes()
        from folio.schema_journal import journal_sql

        if kind == "target":
            admin("BEGIN;" + journal_sql(2, raw) + "COMMIT;", database)
    provision_runtime(names["workbench"], "workbench")
    provision_runtime(names["target"], "target")
    provision_runtime(names["target"], "reader")

    # ---- B. from round-4 backup snapshots ------------------------------------------
    report["B_from_backup"] = {}
    for dump, tag in [
        ("folio_browser_wb_20261026080908.dump", "mgb"),
        ("folio_workbench.dump", "mgm"),
    ]:
        db, owner_dsn, owner = restore_snapshot(dump, stamp, tag)
        rt = provision_runtime(db, "workbench")
        role = runtime_role(rt)
        before_j, before_f, before_x = (
            journal(owner_dsn),
            fingerprints(owner_dsn),
            fn_xmin(owner_dsn),
        )
        first = operator_apply(owner_dsn, role)
        mid_j, mid_f, mid_x = (
            journal(owner_dsn),
            fingerprints(owner_dsn),
            fn_xmin(owner_dsn),
        )
        time.sleep(1)
        second = operator_apply(owner_dsn, role)
        end_j, end_x = journal(owner_dsn), fn_xmin(owner_dsn)
        report["B_from_backup"][dump] = {
            "scratch_database": db,
            "journal_before": [int(v) for v, _, _ in before_j],
            "first_apply": first,
            "journal_after_first": [int(v) for v, _, _ in mid_j],
            "preexisting_journal_rows_untouched": mid_j[: len(before_j)] == before_j,
            "journal_matches_files": all(expected[int(v)] == s for v, s, _ in mid_j),
            "data_tables_unchanged": {t: before_f[t] == mid_f.get(t) for t in before_f},
            "second_apply": second,
            "journal_identical_after_second": end_j == mid_j,
            "fn_004_xmin": [before_x, mid_x, end_x],
            "fn_004_ddl_reexecuted_on_second_apply": mid_x != end_x,
        }
        save()
        if tag == "mgb":
            snap_owner, snap_rt = owner_dsn, rt
        else:
            v2_rt, v2_db = rt, db

    # ---- C. 004 refusal paths on a DB that already has 004 ---------------------------
    c = {}
    x0 = fn_xmin(snap_owner)
    env_rt = {**base_env(), "PYTHONPATH": str(ROOT), "FOLIO_MIGRATION_DSN": snap_rt}
    r = subprocess.run(
        [
            PY,
            str(ROOT / "scripts/export_migrate.py"),
            "--apply",
            "--runtime-role",
            runtime_role(snap_rt),
        ],
        env=env_rt,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    c["runtime_principal_apply"] = {
        "exit": r.returncode,
        "error": (r.stderr.strip().splitlines() or [""])[-1][-160:],
    }
    env_rs = {
        **base_env(),
        "PYTHONPATH": str(ROOT),
        "FOLIO_SCHEMA_MODE": "runtime",
        "FOLIO_DSN": snap_rt,
    }
    r = subprocess.run(
        [
            PY,
            "-c",
            "from folio.workbench import migrate;migrate();print('runtime-check-ok')",
        ],
        env=env_rs,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    c["runtime_startup_migrate"] = {"exit": r.returncode, "stdout": r.stdout.strip()}
    c["fn_004_xmin_unchanged_by_runtime_paths"] = fn_xmin(snap_owner) == x0
    with psycopg.connect(snap_owner) as db:
        good = db.execute(
            "SELECT sha256 FROM schema_versions WHERE version=4"
        ).fetchone()[0]
        db.execute("UPDATE schema_versions SET sha256=%s WHERE version=4", ("0" * 64,))
    tampered = operator_apply(snap_owner, runtime_role(snap_rt))
    with psycopg.connect(snap_owner) as db:
        still = db.execute(
            "SELECT sha256 FROM schema_versions WHERE version=4"
        ).fetchone()[0]
        db.execute("UPDATE schema_versions SET sha256=%s WHERE version=4", (good,))
    c["tampered_journal_apply"] = {
        **tampered,
        "journal_row_left_as_found": still == "0" * 64,
        "fn_004_xmin_unchanged": fn_xmin(snap_owner) == x0,
    }
    report["C_004_refusal"] = c
    save()

    # ---- D. crash midway through an operator migration ------------------------------
    names_d, env_d = create_profile(ROOT, f"mgd{stamp}")
    owner_d = psycopg.conninfo.conninfo_to_dict(env_d["FOLIO_DSN"])["user"]
    r = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "folio-postgres",
            "pg_restore",
            "-U",
            "postgres",
            "--exit-on-error",
            "--no-owner",
            f"--role={owner_d}",
            "-d",
            names_d["workbench"],
        ],
        input=(BACKUP / "folio_workbench.dump").read_bytes(),
        capture_output=True,
    )
    assert r.returncode == 0
    rt_d = provision_runtime(names_d["workbench"], "workbench")
    before = journal(env_d["FOLIO_DSN"]), fingerprints(env_d["FOLIO_DSN"])
    holder = subprocess.Popen(
        [
            "docker",
            "exec",
            "-i",
            "folio-postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            names_d["workbench"],
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    holder.stdin.write(
        b"BEGIN; LOCK TABLE cases IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(40); COMMIT;\n"
    )
    holder.stdin.flush()
    time.sleep(2)
    menv = {
        **base_env(),
        "PYTHONPATH": str(ROOT),
        "FOLIO_MIGRATION_DSN": env_d["FOLIO_DSN"],
    }
    mig = subprocess.Popen(
        [
            PY,
            str(ROOT / "scripts/export_migrate.py"),
            "--apply",
            "--runtime-role",
            runtime_role(rt_d),
        ],
        env=menv,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    blocked = None
    for _ in range(100):
        with psycopg.connect(env_d["FOLIO_DSN"]) as db:
            blocked = db.execute(
                "SELECT left(query,60), wait_event_type FROM pg_stat_activity WHERE datname=current_database() "
                "AND usename=current_user AND pid<>pg_backend_pid() AND wait_event_type='Lock'"
            ).fetchone()
        if blocked:
            break
        time.sleep(0.2)
    psutil.Process(mig.pid).kill()
    mig.wait(10)
    admin(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{names_d['workbench']}' AND pid<>pg_backend_pid()"
    )
    holder.kill()
    time.sleep(1)
    after_crash = journal(env_d["FOLIO_DSN"]), fingerprints(env_d["FOLIO_DSN"])
    with psycopg.connect(env_d["FOLIO_DSN"]) as db:
        partial = db.execute(
            "SELECT to_regclass('public.export_jobs')::text, to_regprocedure(%s)::text",
            (FN,),
        ).fetchone()
    rerun = operator_apply(env_d["FOLIO_DSN"], runtime_role(rt_d))
    report["D_crash_midway"] = {
        "scratch_database": names_d["workbench"],
        "blocked_inside_transaction": bool(blocked),
        "blocked_statement_prefix": blocked[0] if blocked else None,
        "killed_exit": mig.returncode,
        "journal_before": [int(v) for v, _, _ in before[0]],
        "journal_after_crash": [int(v) for v, _, _ in after_crash[0]],
        "partial_objects_after_crash": partial,
        "data_unchanged_after_crash": before[1] == after_crash[1],
        "rerun": rerun,
        "journal_after_rerun": [int(v) for v, _, _ in journal(env_d["FOLIO_DSN"])],
        "data_unchanged_after_rerun": fingerprints(env_d["FOLIO_DSN"]) == before[1],
    }
    save()

    # ---- E. N/N-1 application compatibility ----------------------------------------
    def api_probe(source_root, rt_env, port, label):
        env = {
            **base_env(),
            **rt_env,
            "FOLIO_SCHEMA_MODE": "runtime",
            "PYTHONPATH": str(source_root),
            "FOLIO_EXPORT_ROOT": str(
                (ROOT / ".local/round5" / f"mig{stamp}" / "exports").resolve()
            ),
            "FOLIO_TARGET_URL": "http://127.0.0.1:9291",
        }
        log_path = private_out / f"{label}.log"
        log = log_path.open("w")
        p = subprocess.Popen(
            [
                PY,
                "-B",
                "-m",
                "uvicorn",
                "folio.workbench:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=source_root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        result = {
            "source": str(source_root.relative_to(ROOT))
            if source_root != ROOT
            else "current"
        }
        try:
            ok = False
            for _ in range(60):
                if p.poll() is not None:
                    break
                try:
                    h = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
                    ok = h.status_code == 200
                    result["health"] = h.status_code
                    break
                except httpx.HTTPError:
                    time.sleep(0.5)
            result["process_exited"] = p.poll()
            if ok:
                c = httpx.Client(base_url=f"http://127.0.0.1:{port}")
                lr = c.post(
                    "/api/v1/auth/login",
                    json={"username": "reviewer", "password": "folio-reviewer-local"},
                    headers={"Origin": "http://127.0.0.1:5101"},
                )
                result["login"] = lr.status_code
                case_list = c.get(
                    "/api/v1/workspaces/11111111-1111-4111-8111-111111111111/cases"
                )
                result["case_list"] = case_list.status_code
                if source_root == N1 and lr.status_code == 200 and case_list.status_code == 200:
                    # Exercise the exact N-1 enqueue route after login. Read the
                    # case identity through the same runtime principal, then let
                    # the archived N-1 app perform its immutable direct row lock.
                    with psycopg.connect(rt_env["FOLIO_DSN"]) as db:
                        case = db.execute(
                            "SELECT id::text, body->>'version' FROM cases "
                            "WHERE workspace=%s ORDER BY id LIMIT 1",
                            ("11111111-1111-4111-8111-111111111111",),
                        ).fetchone()
                    if case:
                        session = c.get("/api/v1/auth/session")
                        case_id, case_version = case
                        enqueue = c.post(
                            "/api/v1/workspaces/11111111-1111-4111-8111-111111111111/"
                            f"cases/{case_id}/exports",
                            json={
                                "case_version": int(case_version),
                                "include_originals": False,
                                "redaction_profile": "audit-v1",
                            },
                            headers={
                                "Origin": "http://127.0.0.1:5101",
                                "x-csrf-token": session.json()["csrf"],
                                "idempotency-key": str(uuid4()),
                            },
                        )
                        result["export_enqueue"] = {
                            "status": enqueue.status_code,
                            "response_keys": sorted(enqueue.json())
                            if enqueue.headers.get("content-type", "").startswith(
                                "application/json"
                            )
                            else [],
                        }
                    else:
                        result["export_enqueue"] = {"status": "NO_CASE_FIXTURE"}
        finally:
            if p.poll() is None:
                psutil.Process(p.pid).kill()
            log.close()
        result["startup_error"] = [
            ln
            for ln in log_path.read_text(errors="replace").splitlines()
            if "SCHEMA_MIGRATION_REQUIRED" in ln or "Error" in ln
        ][-1:]
        result["private_log"] = str(log_path.relative_to(ROOT))
        return result

    # Fresh, never-migrated v2-only snapshot (B upgraded its own copy to v4).
    v2_db, _, _ = restore_snapshot("folio_workbench.dump", stamp, "mgv")
    v2_rt = provision_runtime(v2_db, "workbench")
    report["E_compatibility"] = {
        "v2_only_scratch_database": v2_db,
        "v2_only_journal": [int(v) for v, _, _ in journal(v2_rt)],
        "n_minus_1_source": str(N1.relative_to(ROOT)),
        "n_minus_1_has_004": (N1 / "ops/migrations/004-export-actor-lock.sql").exists(),
        "app_N_on_schema_N_from_backup": api_probe(
            ROOT, {"FOLIO_DSN": snap_rt}, 9102, "appN-schemaN"
        ),
        "app_N_minus_1_on_schema_N_from_backup": api_probe(
            N1, {"FOLIO_DSN": snap_rt}, 9103, "appN1-schemaN"
        ),
        "app_N_on_schema_N_minus_1_v2_only": api_probe(
            ROOT, {"FOLIO_DSN": v2_rt}, 9104, "appN-schemaV2"
        ),
    }
    n1_source_after = verify_n1_source_pins()
    if n1_source_after != n1_source_before:
        raise RuntimeError("N1_SOURCE_CHANGED_DURING_DRILL")
    report["immutable_n1_source_integrity"] = {
        "before": n1_source_before,
        "after": n1_source_after,
        "preserved": True,
    }
    save()
    n1_probe = report["E_compatibility"]["app_N_minus_1_on_schema_N_from_backup"]
    if n1_probe.get("export_enqueue", {}).get("status") != 202:
        raise RuntimeError("N1_EXPORT_ENQUEUE_COMPATIBILITY_FAILED")
    print(json.dumps({"report": str(out / "report.json")}))


if __name__ == "__main__":
    main()
