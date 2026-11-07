"""Requester authority read/row-lock capability (additive, operator-installed).

Durable analysis drafts must hold a physical SHARE row lock on the requesting
reviewer's ``users`` row until the preparing transaction ends, so a concurrent
role removal or deactivation serializes behind it. PostgreSQL requires UPDATE
privilege on at least one column for ``FOR SHARE``; the least-privileged runtime
login deliberately has none on ``users`` (SQLSTATE 42501).

Fix, following the reviewed Fieldwork ``checkpoint_read`` pattern: a fresh
NOLOGIN, non-bypass role owns ONE fixed SECURITY DEFINER function in a
dedicated schema. That owner holds column SELECT on (username, active, roles)
plus the single column UPDATE privilege PostgreSQL needs for SHARE; the runtime
login only receives EXECUTE, and the body refuses any other session_user.
The runtime login still has no users UPDATE/INSERT/DELETE. Never adopts an
existing owner role or schema; reinstall is a read-only verification.
"""

import re

from psycopg import sql

SCHEMA = "folio_requester_lock_v1"
SIGNATURE = SCHEMA + ".locked_requester(text)"
LOCK_COLUMN = "active"


def owner_name(login):
    owner = login + "_rq_lock"
    if len(owner.encode()) > 63:
        raise ValueError("REQUESTER_LOCK_OWNER_NAME_TOO_LONG")
    return owner


def body(login):
    # Literal role binding is server-quoted, never caller SQL.
    return sql.SQL(
        """BEGIN
    IF session_user <> {} THEN
        RAISE EXCEPTION 'REQUESTER_LOCK_DENIED' USING ERRCODE='42501';
    END IF;
    RETURN QUERY SELECT u.active, u.roles FROM public.users AS u
        WHERE u.username = p_username
        FOR SHARE OF u NOWAIT;
END"""
    ).format(sql.Literal(login))


def install_sql(login, render):
    """Complete operator SQL (caller wraps in one transaction).

    ``render`` turns a Composable into text (needs a connection or psql quoting).
    """
    owner = owner_name(login)
    o, lg, sc = sql.Identifier(owner), sql.Identifier(login), sql.Identifier(SCHEMA)
    fn = sql.SQL(SCHEMA + ".locked_requester")
    statements = [
        sql.SQL(
            "DO $guard$ BEGIN "
            "IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname={owner_l}) THEN "
            "RAISE EXCEPTION 'EXISTING_REQUESTER_LOCK_OWNER_NOT_ADOPTED'; END IF; "
            "IF EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname={schema_l}) THEN "
            "RAISE EXCEPTION 'EXISTING_REQUESTER_LOCK_SCHEMA_NOT_ADOPTED'; END IF; "
            "IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname={login_l} "
            "AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole AND NOT rolcreatedb "
            "AND NOT rolreplication) THEN RAISE EXCEPTION 'UNSAFE_REQUESTER_RUNTIME_ROLE'; END IF; "
            "END $guard$;"
        ).format(
            owner_l=sql.Literal(owner),
            schema_l=sql.Literal(SCHEMA),
            login_l=sql.Literal(login),
        ),
        sql.SQL(
            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOINHERIT;"
        ).format(o),
        sql.SQL("CREATE SCHEMA {};").format(sc),
        sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC;").format(sc),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {};").format(o),
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {};").format(sc, lg),
        sql.SQL("GRANT SELECT(username,active,roles) ON public.users TO {};").format(o),
        sql.SQL("GRANT UPDATE({}) ON public.users TO {};").format(
            sql.Identifier(LOCK_COLUMN), o
        ),
        sql.SQL(
            "CREATE FUNCTION {}(p_username text) RETURNS TABLE(active boolean, roles jsonb) "
            "LANGUAGE plpgsql VOLATILE SECURITY DEFINER "
            "SET search_path=pg_catalog,pg_temp AS {};"
        ).format(fn, sql.Literal(render(body(login)))),
        sql.SQL("ALTER FUNCTION {}(text) OWNER TO {};").format(fn, o),
        sql.SQL("REVOKE ALL ON FUNCTION {}(text) FROM PUBLIC;").format(fn),
        sql.SQL("GRANT EXECUTE ON FUNCTION {}(text) TO {};").format(fn, lg),
    ]
    return "\n".join(render(s) for s in statements)


def _fixed_path(config):
    if not config or len(config) != 1:
        return False
    return (
        re.fullmatch(r"search_path=[ \t]*pg_catalog[ \t]*,[ \t]*pg_temp[ \t]*", config[0])
        is not None
    )


def verify(c, login):
    """Fail closed on changed body/owner/path, memberships, PUBLIC execute or
    any runtime write privilege on users. Read-only; any principal may run it."""
    owner = owner_name(login)
    flags = "rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication"
    runtime = c.execute(
        f"SELECT {flags} FROM pg_catalog.pg_roles WHERE rolname=%s", (login,)
    ).fetchone()
    if not runtime or any(runtime):
        raise ValueError("UNSAFE_REQUESTER_RUNTIME_ROLE")
    row = c.execute(
        f"SELECT {flags},rolcanlogin FROM pg_catalog.pg_roles WHERE rolname=%s",
        (owner,),
    ).fetchone()
    if not row or any(row):
        raise ValueError("UNSAFE_REQUESTER_LOCK_OWNER")
    if c.execute(
        "SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r "
        "ON r.oid=m.member OR r.oid=m.roleid WHERE r.rolname=%s",
        (owner,),
    ).fetchone():
        raise ValueError("REQUESTER_LOCK_ROLE_MEMBERSHIP")
    fn = c.execute(
        "SELECT pg_catalog.pg_get_userbyid(proowner),prosecdef,proconfig,prosrc "
        "FROM pg_catalog.pg_proc WHERE oid=pg_catalog.to_regprocedure(%s)",
        (SIGNATURE,),
    ).fetchone()
    if (
        not fn
        or fn[0] != owner
        or fn[1] is not True
        or not _fixed_path(fn[2])
        or fn[3] != body(login).as_string(c)
    ):
        raise ValueError("REQUESTER_LOCK_FUNCTION_MISMATCH")
    if c.execute(
        "SELECT 1 FROM pg_catalog.pg_proc p, LATERAL pg_catalog.aclexplode("
        "COALESCE(p.proacl, pg_catalog.acldefault('f',p.proowner))) a "
        "WHERE p.oid=pg_catalog.to_regprocedure(%s) AND a.grantee=0 "
        "AND a.privilege_type='EXECUTE'",
        (SIGNATURE,),
    ).fetchone():
        raise ValueError("REQUESTER_LOCK_PUBLIC_EXECUTE")
    for column in ("username", "active", "roles"):
        if not c.execute(
            "SELECT pg_catalog.has_column_privilege(%s,'public.users',%s,'SELECT')",
            (owner, column),
        ).fetchone()[0]:
            raise ValueError("REQUESTER_LOCK_OWNER_ACCESS_REQUIRED")
    if not c.execute(
        "SELECT pg_catalog.has_any_column_privilege(%s,'public.users','UPDATE')",
        (owner,),
    ).fetchone()[0]:
        raise ValueError("REQUESTER_LOCK_OWNER_ACCESS_REQUIRED")
    for permission in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        if c.execute(
            "SELECT pg_catalog.has_table_privilege(%s,'public.users',%s)",
            (login, permission),
        ).fetchone()[0]:
            raise ValueError("REQUESTER_RUNTIME_USERS_WRITE_FORBIDDEN")
    for permission in ("INSERT", "UPDATE"):
        if c.execute(
            "SELECT pg_catalog.has_any_column_privilege(%s,'public.users',%s)",
            (login, permission),
        ).fetchone()[0]:
            raise ValueError("REQUESTER_RUNTIME_USERS_WRITE_FORBIDDEN")
    if not c.execute(
        "SELECT pg_catalog.has_function_privilege(%s,%s,'EXECUTE')",
        (login, SIGNATURE),
    ).fetchone()[0]:
        raise ValueError("REQUESTER_LOCK_EXECUTE_REQUIRED")
    return {"schema": SCHEMA, "owner": owner, "login": login, "verified": True}


def available(db):
    """True only when the current principal may execute the installed helper."""
    row = db.execute(
        "SELECT COALESCE(pg_catalog.has_function_privilege(current_user,"
        "pg_catalog.to_regprocedure(%s),'EXECUTE'),false)",
        (SIGNATURE,),
    ).fetchone()
    return bool(row) and row[0] is True
