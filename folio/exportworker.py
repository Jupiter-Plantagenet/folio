"""Bounded export worker: one DB lease, one <=256KiB private file, fenced publish.
No target/provider code imported or called. Execute explicitly with --once.
"""

import argparse
import hashlib
import os
from contextlib import contextmanager
from uuid import uuid4

from folio.exports import MAX_BYTES, canonical, read_bytes, root
from folio.workbench import connect as _connect
from folio.workbench import event, load_case

MAINTENANCE_LOCK_KEY = "folio-paired-backup-admission-v1"


def maintenance_requested():
    from folio.maintenance_gate import marker_present

    return marker_present()


@contextmanager
def connect():
    from folio.restore_mode import require_writable

    with _connect() as db:
        require_writable(db)
        yield db


def claim(job_id=None):
    with connect() as db:
        # Transaction-level share lock makes the marker check atomic with the
        # supervisor's session-level exclusive paired-backup admission barrier.
        db.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
                   (MAINTENANCE_LOCK_KEY,))
        if maintenance_requested():
            return None
        db.execute("SET LOCAL lock_timeout='2s'")
        stranded = db.execute(
            "SELECT id,case_id FROM export_jobs WHERE state='leased' AND tries>=5 "
            "AND lease_until<clock_timestamp() ORDER BY id LIMIT 20 FOR UPDATE SKIP LOCKED"
        ).fetchall()
        for identifier, case_id in stranded:
            db.execute(
                "UPDATE export_jobs SET state='exhausted' WHERE id=%s", (identifier,)
            )
            event(
                db,
                str(case_id),
                "DOC-job-changed",
                "export-worker",
                {"artifact_job_id": str(identifier), "state": "exhausted"},
            )
        args = [job_id] if job_id else []
        row = db.execute(
            "SELECT id,workspace,case_id,fence,snapshot,snapshot_sha256 FROM export_jobs "
            "WHERE (state='queued' OR (state='leased' AND lease_until<clock_timestamp())) "
            "AND tries<5 AND expires_at>clock_timestamp() "
            + ("AND id=%s " if job_id else "")
            + "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
            args,
        ).fetchone()
        if not row:
            return None
        attempt, fence = str(uuid4()), row[3] + 1
        db.execute(
            "UPDATE export_jobs SET state='leased',attempt=%s,fence=%s,tries=tries+1,"
            "lease_until=clock_timestamp()+interval '30 seconds' WHERE id=%s",
            (attempt, fence, row[0]),
        )
        return dict(
            id=str(row[0]),
            workspace=str(row[1]),
            case_id=str(row[2]),
            attempt=attempt,
            fence=fence,
            snapshot=row[4],
            digest=row[5],
        )


def stage(j):
    raw = canonical(j["snapshot"])
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != j["digest"]:
        raise RuntimeError("SNAPSHOT_INTEGRITY_FAILED")
    name = j["id"] + "." + j["attempt"]
    path = root() / (name + ".stage")
    with path.open("xb") as f:
        os.chmod(path, 0o600)
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    return path, name + ".json", len(raw)


def publish(j, ref, length):
    read_bytes(ref, j["digest"], length)
    with connect() as db:
        db.execute("SET LOCAL lock_timeout='2s'")
        row = db.execute(
            "UPDATE export_jobs SET state='ready',artifact_ref=%s,artifact_sha256=%s,"
            "artifact_bytes=%s,lease_until=NULL WHERE id=%s AND workspace=%s AND state='leased' "
            "AND attempt=%s AND fence=%s AND lease_until>clock_timestamp() "
            "AND expires_at>clock_timestamp() RETURNING case_id",
            (
                ref,
                j["digest"],
                length,
                j["id"],
                j["workspace"],
                j["attempt"],
                j["fence"],
            ),
        ).fetchone()
        if not row:
            raise RuntimeError("FENCE_OR_LEASE_LOST")
        event(
            db,
            str(row[0]),
            "DOC-artifact-ready",
            "export-worker",
            {"artifact_job_id": j["id"], "sha256": j["digest"], "fence": j["fence"]},
        )
        event(
            db,
            str(row[0]),
            "DOC-job-changed",
            "export-worker",
            {"artifact_job_id": j["id"], "state": "ready"},
        )


def run_once(job_id=None, checkpoint=None):
    j = claim(job_id)
    if not j:
        return False
    checkpoint = checkpoint or (lambda _: None)
    checkpoint("before_file")
    path, ref, length = stage(j)
    checkpoint("after_stage")
    os.replace(path, root() / ref)
    checkpoint("before_ref")
    publish(j, ref, length)
    checkpoint("after_commit")
    return True


def expire_one(job_id):
    """Tombstone only this due export; legal hold preserves bytes but TTL denies access.
    No source, target operation, command journal or frozen snapshot is modified.
    Private archival follows committed denial; evidence bytes are never deleted.
    """
    if maintenance_requested():
        return False
    with connect() as db:
        ref = db.execute(
            "SELECT workspace,case_id FROM export_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        if not ref:
            return False
        c = load_case(db, str(ref[0]), str(ref[1]))
        row = db.execute(
            "SELECT artifact_ref,state,expires_at<=clock_timestamp() FROM export_jobs "
            "WHERE id=%s FOR UPDATE",
            (job_id,),
        ).fetchone()
        retention = c.get("retention", {})
        if not row[2] or retention.get("legal_hold") or retention.get("pinned_release_evidence"):
            return False
        db.execute(
            "UPDATE export_jobs SET state='tombstoned',deleted_at=coalesce(deleted_at,clock_timestamp()) "
            "WHERE id=%s",
            (job_id,),
        )
        if row[1] != "tombstoned":
            event(
                db,
                str(ref[1]),
                "DOC-job-changed",
                "export-worker",
                {"artifact_job_id": job_id, "state": "tombstoned"},
            )
    # Bound cleanup to this canonical job's maximum five attempts, including crash orphans.
    from folio.exports import identifier

    identifier(job_id)
    from itertools import islice

    candidates = list(islice(root().glob(job_id + ".*"), 12))
    if len(candidates) > 10:
        raise RuntimeError("ARTIFACT_CLEANUP_BOUND_EXCEEDED")
    # Reacquire the case lock for the filesystem change: a newly applied legal
    # hold must preserve bytes even after the access-denying tombstone committed.
    with connect() as db:
        c = load_case(db, str(ref[0]), str(ref[1]))
        retention = c.get("retention", {})
        if retention.get("legal_hold") or retention.get("pinned_release_evidence"):
            return False
        archive_parent = root() / ".expired-private"
        archive = archive_parent / job_id
        from folio.private_storage import secure_private_directory

        for folder in (archive_parent, archive):
            secure_private_directory(folder)
            metadata = folder.lstat()
            if folder.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise RuntimeError("ARTIFACT_ARCHIVE_REPARSE_POINT")
        for path in candidates:
            parts = path.name.split(".")
            if len(parts) != 3 or parts[2] not in {"stage", "json"} or path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise RuntimeError("ARTIFACT_REFERENCE_INVALID")
            identifier(parts[1])
            destination = archive / path.name
            # Never overwrite a previous archive, including a recovered duplicate.
            if destination.exists():
                destination = archive / (str(uuid4()) + "." + path.name)
            path.rename(destination)
            from folio.private_storage import secure_private_file

            secure_private_file(destination)
    return True


def expire_due(limit=20, *, workspace="11111111-1111-4111-8111-111111111111"):
    """Expire one bounded batch of due exports; callers control scheduling."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("RETENTION_BATCH_LIMIT_INVALID")
    if workspace != "11111111-1111-4111-8111-111111111111":
        raise ValueError("RETENTION_WORKSPACE_SCOPE_REFUSED")
    if maintenance_requested():
        raise RuntimeError("RETENTION_MAINTENANCE_ACTIVE")
    with connect() as db:
        from folio.retention_backend import _assert_runtime_identity

        _assert_runtime_identity(db)
        due = db.execute(
            "SELECT e.id FROM export_jobs e "
            "JOIN cases c ON c.workspace=e.workspace AND c.id=e.case_id "
            "WHERE e.workspace=%s AND e.expires_at<=clock_timestamp() AND e.state!='tombstoned' "
            "AND coalesce(c.body->'retention'->>'legal_hold','false')!='true' "
            "AND coalesce(c.body->'retention'->>'pinned_release_evidence','false')!='true' "
            "ORDER BY e.expires_at,e.id LIMIT %s",
            (workspace, limit),
        ).fetchall()
    # expire_one rechecks expiry and hold under the case lock, then commits the
    # tombstone before archival. Never trust this discovery query as authorization.
    return sum(1 for (job_id,) in due if expire_one(str(job_id)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--job")
    parser.add_argument("--expire")
    parser.add_argument("--expire-due", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.expire_due:
        if args.job or args.expire:
            parser.error("--expire-due cannot be combined with --job or --expire")
        try:
            count = expire_due(args.limit)
        except ValueError as error:
            parser.error(str(error))
        print(f"expired={count} limit={args.limit}")
        return
    if args.limit != 20:
        parser.error("--limit is only valid with --expire-due")
    if args.job:
        from folio.exports import identifier

        identifier(args.job)
    result = expire_one(args.expire) if args.expire else run_once(args.job)
    print("processed" if result else "no-eligible-job")


if __name__ == "__main__":
    main()
