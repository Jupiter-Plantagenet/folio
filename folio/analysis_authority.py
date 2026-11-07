"""Requester checks for durable model drafts; never grants auth-table privileges."""
from psycopg.errors import InsufficientPrivilege, LockNotAvailable

from folio.requester_lock import SIGNATURE, available


def requester_problem(db, username):
    """Lock current authority until the caller's transaction ends.

    A savepoint is essential: a permission denial must not poison the outer
    transaction that records the blocked run. Row locks taken inside a released
    savepoint remain held by the enclosing transaction. This is NOT the
    session-token export helper, and it does not relax the row-lock requirement.

    The least-privileged runtime login has no users UPDATE, so a direct
    ``FOR SHARE`` is denied (42501). When the operator-installed requester
    capability (``folio.requester_lock``) is executable by the current
    principal, the identical ``FOR SHARE NOWAIT`` lock is taken through that
    fixed SECURITY DEFINER function; otherwise (owner/bootstrap principals) the
    direct lock is attempted and a denial still fails closed.
    """
    try:
        with db.transaction():
            if available(db):
                actor = db.execute(
                    'SELECT active,roles FROM ' + SIGNATURE.replace('(text)', '(%s)'),
                    (username,),
                ).fetchone()
            else:
                actor = db.execute(
                    'SELECT active,roles FROM users WHERE username=%s FOR SHARE NOWAIT',
                    (username,),
                ).fetchone()
    except InsufficientPrivilege:
        return 'REQUESTER_LOCK_CAPABILITY_REQUIRED'
    except LockNotAvailable:
        return 'REQUESTER_LOCK_BUSY'
    if (
        not actor
        or actor[0] is not True
        or type(actor[1]) is not list
        or 'reviewer' not in actor[1]
    ):
        return 'REQUESTER_AUTHORITY_REVOKED'
    return None
