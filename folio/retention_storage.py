"""Database operations shared by explicit and scheduled original-byte deletion."""


def delete_original_rows(db, workspace, case_id, *, expired_only=False):
    """Remove only the permitted bytes and return the actual stored tombstones.

    The caller must already hold the case lock and check holds/proposal gates.
    Scheduled retention rechecks age in PostgreSQL at the mutation itself; an
    old document in a case must never cause newer documents to be deleted.
    Explicit authorized deletion intentionally has no age restriction.
    """
    if type(expired_only) is not bool:
        raise ValueError('EXPIRY_MODE_REQUIRED')
    query = (
        'UPDATE originals SET raw=NULL,deleted_at=clock_timestamp() '
        'WHERE workspace=%s AND case_id=%s AND raw IS NOT NULL '
    )
    if expired_only:
        query += "AND created_at < clock_timestamp()-interval '90 days' "
    query += 'RETURNING id,sha256'
    return db.execute(query, (workspace, case_id)).fetchall()
