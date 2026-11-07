"""Bounded PostgreSQL connection pool for request paths (round 6).

Why: the API used to open one TCP connection (plus SCRAM authentication) per request and
per identity check. Under 20 concurrent users Windows ran out of ephemeral client ports
(`OperationalError ... (10048)`), and connection setup dominated read latency.

Contract of one lease (``with pool.lease(workspace) as db:``):

* exactly one database transaction: committed when the block exits normally, rolled back
  when it raises (the same semantics as ``with psycopg.connect(...) as db``);
* a transaction-local tenant context ``folio.workspace`` set from the caller's trusted
  membership (``set_config(..., true)``); it ends with the transaction and can never be
  observed by a later lease;
* on return, the session is reset with ``DISCARD ALL`` (session GUCs, ``SET ROLE`` /
  ``SET SESSION AUTHORIZATION``, temp tables, prepared statements, cursors, ``LISTEN``,
  session advisory locks). Any connection that is broken, not idle, or whose reset fails
  is closed instead of being reused;
* bounded: at most ``max_size`` connections per DSN. A caller waits at most ``timeout``
  seconds and then gets ``PoolTimeout``; nothing is opened beyond the bound.

The pool never hands one connection to two leases at once, and a lease never sees state
written by a previous lease except committed rows (which RLS still scopes).
"""

import threading
import time
from contextlib import contextmanager

import psycopg
from psycopg import pq

TENANT_GUC = "folio.workspace"


class PoolTimeout(Exception):
    """No connection became available within the pool timeout."""


class Pool:
    def __init__(self, dsn, max_size=8, timeout=5.0, max_lifetime=1800.0, name="folio"):
        if max_size < 1:
            raise ValueError("POOL_MAX_SIZE_INVALID")
        self.dsn = dsn
        self.max_size = max_size
        self.timeout = timeout
        self.max_lifetime = max_lifetime
        self.name = name
        self._idle = []  # LIFO of idle connections
        self._opened = {}  # id(connection) -> opened_monotonic, for leased connections too
        self._size = 0
        self._cond = threading.Condition()
        self._stats_lock = threading.Lock()
        self.stats = {"opened": 0, "closed": 0, "leases": 0, "timeouts": 0, "discarded": 0}

    def _count(self, key):
        with self._stats_lock:
            self.stats[key] += 1

    # -- connection lifecycle -------------------------------------------------------
    def _open(self):
        # prepare_threshold=None: DISCARD ALL deallocates server-side prepared statements,
        # so never let psycopg rely on any.
        conn = psycopg.connect(
            self.dsn, prepare_threshold=None, application_name=self.name + "-pool"
        )
        self._opened[id(conn)] = time.monotonic()
        self._count("opened")
        return conn

    def _close(self, conn):
        self._opened.pop(id(conn), None)
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing a dead socket must not mask the caller
            pass
        self._count("closed")
        with self._cond:
            self._size -= 1
            self._cond.notify()

    def _usable(self, conn):
        opened = self._opened.get(id(conn), 0)
        return (
            not conn.closed
            and not conn.broken
            and conn.info.transaction_status == pq.TransactionStatus.IDLE
            and time.monotonic() - opened < self.max_lifetime
        )

    def _checkout(self):
        deadline = time.monotonic() + self.timeout
        with self._cond:
            while True:
                while self._idle:
                    conn = self._idle.pop()
                    if self._usable(conn):
                        return conn
                    # Defer the close outside the condition (it re-acquires it).
                    self._cond.release()
                    try:
                        self._count("discarded")
                        self._close(conn)
                    finally:
                        self._cond.acquire()
                if self._size < self.max_size:
                    self._size += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._count("timeouts")
                    raise PoolTimeout(self.name)
                self._cond.wait(remaining)
        try:
            return self._open()
        except BaseException:
            with self._cond:
                self._size -= 1
                self._cond.notify()
            raise

    def _checkin(self, conn, commit):
        """End the lease's transaction, reset the session and return or close it.

        A failed COMMIT is re-raised to the caller (the request must not report success);
        every other reset failure just retires the connection.
        """
        try:
            if commit:
                conn.commit()
            else:
                conn.rollback()
        except BaseException:
            self._count("discarded")
            self._close(conn)
            if commit:
                raise
            return
        try:
            conn.autocommit = True
            conn.execute("DISCARD ALL")
            conn.autocommit = False
            if not self._usable(conn):
                raise RuntimeError("POOL_RESET_NOT_IDLE")
        except Exception:  # noqa: BLE001 - retire anything we cannot prove clean
            self._count("discarded")
            self._close(conn)
            return
        with self._cond:
            self._idle.append(conn)
            self._cond.notify()

    # -- public API -----------------------------------------------------------------
    @contextmanager
    def lease(self, workspace):
        conn = self._checkout()
        try:
            conn.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, workspace))
        except psycopg.OperationalError:
            # A pooled socket can be stale after a database restart. Nothing of the
            # caller's has run yet, so replace it once with a fresh connection.
            self._count("discarded")
            self._close(conn)
            conn = self._checkout()
            try:
                conn.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, workspace))
            except BaseException:
                self._close(conn)
                raise
        self._count("leases")
        try:
            yield conn
        except BaseException:
            self._checkin(conn, commit=False)
            raise
        self._checkin(conn, commit=True)

    def close(self):
        with self._cond:
            idle, self._idle = self._idle, []
        for conn in idle:
            self._close(conn)

    def snapshot(self):
        with self._cond:
            return {
                "max_size": self.max_size,
                "open": self._size,
                "idle": len(self._idle),
                **self.stats,
            }


_pools = {}
_pools_lock = threading.Lock()


def pool_for(dsn, max_size, timeout, name="folio"):
    """One pool per (process, DSN). Tests that swap the DSN get a separate pool."""
    with _pools_lock:
        pool = _pools.get(dsn)
        if pool is None:
            pool = _pools[dsn] = Pool(dsn, max_size=max_size, timeout=timeout, name=name)
        return pool


def close_all():
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()
