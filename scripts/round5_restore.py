"""Round-5 paired restore drill of the ROUND-4 retained backups into NEW scratch databases.

Never restores over a live database. Order: target first, then workbench (blueprint s11).
No worker/dispatcher is ever started against the restored pair (dispatch stays disabled).
Live databases are only READ (row counts) to measure the data-loss window (RPO).
Output: evidence/documents/restore/round5-<stamp>/report.json (no secrets, no row content).
"""

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / ".local/backups/round4-20261030T143651Z"
BACKUP_UTC = datetime(2026, 10, 30, 14, 36, 51, tzinfo=timezone.utc)
MIGRATIONS = {2: None, 3: "003-exports.sql", 4: "004-export-actor-lock.sql"}
PAIRS = {
    "browser": ("folio_browser_target_20261026080908", "folio_browser_wb_20261026080908"),
    "main": ("folio_target", "folio_workbench"),
}


def psql(db, query):
    r = subprocess.run(
        ["docker", "exec", "-i", "folio-postgres", "psql", "-U", "postgres", "-d", db, "-At", "-v", "ON_ERROR_STOP=1"],
        input=query.encode(), capture_output=True,
    )
    if r.returncode:
        raise RuntimeError("PSQL_FAILED:" + r.stderr.decode(errors="replace")[:300])
    return r.stdout.decode().strip()


def jq(db, query):
    out = psql(db, f"SELECT coalesce(json_agg(x),'[]'::json) FROM ({query}) x")
    return json.loads(out)


def fingerprint(db):
    tables = [t["tablename"] for t in jq(db, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")]
    result = {}
    for t in tables:
        row = jq(db, f'SELECT count(*) AS n, md5(coalesce(string_agg(t::text, \'\' ORDER BY t::text),\'\')) AS h FROM public."{t}" t')[0]
        result[t] = row
    return result


def restore(source, dump, stamp):
    name = f"folio_restore_r5_{'target' if 'target' in source else 'wb'}_{source.split('_')[1]}_{stamp}"
    psql("postgres", f'CREATE DATABASE "{name}"')
    r = subprocess.run(
        ["docker", "exec", "-i", "folio-postgres", "pg_restore", "-U", "postgres", "--exit-on-error", "-d", name],
        input=dump.read_bytes(), capture_output=True,
    )
    if r.returncode:
        raise RuntimeError("RESTORE_FAILED:" + r.stderr.decode(errors="replace")[:300])
    return name


def integrity(db, kind):
    checks = {"unvalidated_constraints": int(psql(db, "SELECT count(*) FROM pg_constraint WHERE NOT convalidated"))}
    journal = jq(db, "SELECT version,sha256 FROM schema_versions ORDER BY version")
    files = {2: f"002-{kind}.sql", 3: "003-exports.sql", 4: "004-export-actor-lock.sql"}
    checks["journal"] = [
        {"version": j["version"], "matches_file": j["sha256"] == hashlib.sha256((ROOT / "ops/migrations" / files[j["version"]]).read_bytes()).hexdigest()}
        for j in journal
    ]
    if kind == "workbench":
        checks["orphans"] = jq(db, """
          SELECT 'proposals->cases' AS fk, count(*) AS n FROM proposals p WHERE NOT EXISTS (SELECT 1 FROM cases c WHERE c.workspace=p.workspace AND c.id=p.case_id)
          UNION ALL SELECT 'jobs->proposals', count(*) FROM jobs j WHERE NOT EXISTS (SELECT 1 FROM proposals p WHERE p.id=j.proposal)
          UNION ALL SELECT 'originals->cases', count(*) FROM originals o WHERE NOT EXISTS (SELECT 1 FROM cases c WHERE c.workspace=o.workspace AND c.id=o.case_id)
          UNION ALL SELECT 'analysis_runs->cases', count(*) FROM analysis_runs a WHERE NOT EXISTS (SELECT 1 FROM cases c WHERE c.workspace=a.workspace AND c.id=a.case_id)
          UNION ALL SELECT 'case.proposal->proposals', count(*) FROM cases c WHERE c.body->'proposal'->>'id' IS NOT NULL AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.id::text=c.body->'proposal'->>'id')""")
        checks["original_hashes"] = jq(db, """
          SELECT count(*) FILTER (WHERE raw IS NOT NULL) AS with_bytes,
                 count(*) FILTER (WHERE raw IS NOT NULL AND encode(sha256(raw),'hex')<>sha256) AS mismatched,
                 count(*) FILTER (WHERE raw IS NULL AND deleted_at IS NOT NULL) AS deleted_tombstones,
                 count(*) FILTER (WHERE raw IS NULL AND deleted_at IS NULL) AS missing_without_tombstone
          FROM originals""")[0]
        checks["event_sequence"] = jq(db, "SELECT count(*) AS n, min(sequence) AS lo, max(sequence) AS hi, count(DISTINCT sequence) AS distinct_n FROM events")[0]
        if psql(db, "SELECT to_regclass('public.export_jobs') IS NOT NULL") == "t":
            checks["orphans"] += jq(db, "SELECT 'export_jobs->cases' AS fk, count(*) AS n FROM export_jobs e WHERE NOT EXISTS (SELECT 1 FROM cases c WHERE c.workspace=e.workspace AND c.id=e.case_id)")
            checks["export_artifacts"] = jq(db, "SELECT state, count(*) AS n FROM export_jobs GROUP BY state ORDER BY 1")
        else:
            checks["export_artifacts"] = "export_jobs absent (schema v2 database)"
    else:
        checks["orphans"] = jq(db, """
          SELECT 'operations->payables' AS fk, count(*) AS n FROM operations o WHERE NOT EXISTS (SELECT 1 FROM payables p WHERE p.workspace=o.workspace AND p.id=o.target_id)
          UNION ALL SELECT 'operations->authority', count(*) FROM operations o WHERE NOT EXISTS (SELECT 1 FROM authority a WHERE a.workspace=o.workspace AND a.operation_key=o.operation_key)""")
        checks["receipt_digests"] = jq(db, """
          SELECT count(*) AS operations,
            count(*) FILTER (WHERE (body->>'after_revision')::int=(body->>'before_revision')::int+1) AS single_increment
          FROM operations""")[0]
    return checks


def sha(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def reconcile(wb, target):
    proposals = jq(wb, """SELECT p.id, p.body->>'operation_key' AS key, p.body->>'target_digest' AS digest,
        p.body->>'state' AS state, p.body->>'gate' AS gate, p.body->>'target' AS target,
        (SELECT j.state FROM jobs j WHERE j.proposal=p.id LIMIT 1) AS job_state FROM proposals p""")
    ops = jq(target, "SELECT operation_key AS key, target_id, body FROM operations")
    payables = {p["id"]: p["body"] for p in jq(target, "SELECT id, body FROM payables")}
    by_key = {o["key"]: o for o in ops}
    rows, wb_keys = [], set()
    for p in proposals:
        wb_keys.add(p["key"])
        o = by_key.get(p["key"])
        claims_applied = p["gate"] == "applied" or p["job_state"] == "completed"
        in_flight = p["gate"] in ("seal_pending", "sealed", "uncertain") or p["job_state"] in ("queued", "leased", "uncertain", "exhausted")
        if o:
            receipt = o["body"]
            ok = (receipt["intent_digest"] == p["digest"] and sha(receipt["before_snapshot"]) == receipt["before_digest"]
                  and sha(receipt["after_snapshot"]) == receipt["after_digest"])
            verdict = "consistent_applied" if (ok and claims_applied) else (
                "target_newer_preserve_target_truth" if ok and in_flight else "receipt_mismatch")
        else:
            verdict = "inconsistent_workbench_claims_effect_target_absent" if claims_applied else (
                "in_flight_unresolved_keep_read_only" if in_flight else "no_effect_expected")
        rows.append({"proposal": p["id"], "state": p["state"], "gate": p["gate"], "job": p["job_state"], "verdict": verdict})
    foreign = [o["key"] for o in ops if o["key"] not in wb_keys]
    # Revision accounting on the target alone: every applied op is exactly one increment.
    revision_breaks = []
    for rid, body in payables.items():
        mine = sorted((o["body"] for o in ops if o["target_id"] == rid), key=lambda r: r["before_revision"])
        chain_ok = all(a["after_revision"] == b["before_revision"] for a, b in zip(mine, mine[1:]))
        if mine and (not chain_ok or mine[-1]["after_revision"] > body["revision"]):
            revision_breaks.append(rid)
    summary = {}
    for r in rows:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    return {"proposals": len(proposals), "target_operations": len(ops), "verdicts": summary,
            "target_ops_without_workbench_proposal": len(foreign), "revision_chain_breaks": len(revision_breaks),
            "rows": rows}


def live_drift(profile, restored_fp, kind):
    """READ-ONLY counts on the live DB to measure what a restore to this backup would lose."""
    profiles = json.loads((ROOT / ".local/database-profiles.json").read_text())
    dsn = profiles[profile]["FOLIO_DSN" if kind == "workbench" else "FOLIO_TARGET_READ_DSN"]
    drift = {}
    try:
        with psycopg.connect(dsn, connect_timeout=5) as db:
            db.execute("SET default_transaction_read_only = on")
            for t, v in restored_fp.items():
                if t in ("sessions", "target_sessions"):
                    continue
                try:
                    n = db.execute(f'SELECT count(*) FROM public."{t}"').fetchone()[0]
                    drift[t] = {"live": n, "restored": v["n"], "rows_after_backup": n - v["n"]}
                except psycopg.Error as e:
                    db.rollback()
                    drift[t] = {"unreadable_by_runtime_principal": type(e).__name__}
            if kind == "workbench":
                drift["_events_after_backup"] = db.execute(
                    "SELECT count(*) FROM events WHERE at > %s", (BACKUP_UTC,)).fetchone()[0]
    except psycopg.Error as e:
        drift = {"error": type(e).__name__}
    return drift


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    out = ROOT / "evidence/documents/restore" / f"round5-{stamp}"
    out.mkdir(parents=True)
    t0 = time.monotonic()
    report = {"backup": BACKUP.name, "backup_utc": BACKUP_UTC.isoformat(), "drill_started_utc": datetime.now(timezone.utc).isoformat(),
              "dispatch_enabled_on_restore": False, "pairs": {}}
    sums = {}
    for line in (BACKUP / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(" *")
        sums[name] = digest
    report["checksums"] = {n: hashlib.sha256((BACKUP / n).read_bytes()).hexdigest() == d for n, d in sums.items()}
    if not all(report["checksums"].values()):
        raise SystemExit("BACKUP_CHECKSUM_MISMATCH")
    for profile, (tsrc, wsrc) in PAIRS.items():
        p0 = time.monotonic()
        tdb = restore(tsrc, BACKUP / f"{tsrc}.dump", stamp)       # target first
        wdb = restore(wsrc, BACKUP / f"{wsrc}.dump", stamp)
        restored_s = time.monotonic() - p0
        # Restored sessions are reset: no restored browser/target session can be replayed.
        psql(wdb, "DELETE FROM sessions")
        psql(tdb, "DELETE FROM target_sessions")
        fp_t, fp_w = fingerprint(tdb), fingerprint(wdb)
        pair = {
            "restored_databases": {"target": tdb, "workbench": wdb},
            "restore_seconds": restored_s,
            "fingerprints": {"target": fp_t, "workbench": fp_w},
            "integrity": {"target": integrity(tdb, "target"), "workbench": integrity(wdb, "workbench")},
            "reconciliation": reconcile(wdb, tdb),
            "sessions_reset": True,
            "pair_rto_seconds": time.monotonic() - p0,
        }
        pair["live_drift_since_backup"] = {"workbench": live_drift(profile, fp_w, "workbench"),
                                           "target": live_drift(profile, fp_t, "target")}
        report["pairs"][profile] = pair
        (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    report["rto_seconds_total"] = time.monotonic() - t0
    report["backup_age_at_drill_hours"] = (datetime.now(timezone.utc) - BACKUP_UTC).total_seconds() / 3600
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({"report": str(out / "report.json"), "rto_s": report["rto_seconds_total"]}))


if __name__ == "__main__":
    sys.exit(main())
