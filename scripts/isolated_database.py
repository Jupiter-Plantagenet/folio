"""Private throwaway PostgreSQL identities for isolated proofs, no live role reuse."""

import json
import secrets
from pathlib import Path

from psycopg import sql

from folio.database_security import admin


def create_profile(root, stamp):
    folder = Path(root) / ".local/isolated" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    roles = {
        kind: {
            "name": f"folio_test_{kind}_{stamp}",
            "password": secrets.token_urlsafe(32),
        }
        for kind in ["wb", "target", "reader"]
    }
    for role in roles.values():
        admin(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;"
            )
            .format(sql.Identifier(role["name"]), sql.Literal(role["password"]))
            .as_string()
        )
    names = {
        "workbench": "folio_finish_wb_" + stamp,
        "target": "folio_finish_target_" + stamp,
    }
    for kind, role in [("workbench", roles["wb"]), ("target", roles["target"])]:
        admin(
            sql.SQL("CREATE DATABASE {} OWNER {};")
            .format(sql.Identifier(names[kind]), sql.Identifier(role["name"]))
            .as_string()
        )

    def dsn(role, database):
        return (
            f"postgresql://{role['name']}:{role['password']}@127.0.0.1:5541/{database}"
        )

    env = {
        "FOLIO_DSN": dsn(roles["wb"], names["workbench"]),
        "FOLIO_TARGET_DSN": dsn(roles["target"], names["target"]),
        "FOLIO_TARGET_READ_DSN": dsn(roles["reader"], names["target"]),
    }
    (folder / "databases.json").write_text(json.dumps(env))
    return names, env
