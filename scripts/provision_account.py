"""Operator-only initial account provisioning; never takes or prints passwords on argv."""

import argparse
import getpass
import hashlib
import hmac
import os
import re
import secrets
import sys

import psycopg
from psycopg.types.json import Jsonb

WORKSPACE = "11111111-1111-4111-8111-111111111111"

PROVISIONABLE_ROLES = {"reviewer", "viewer", "auditor"}
USERNAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._@+-]{1,127}$")


def validate_bootstrap_approval(requester, approver, target):
    return bool(
        requester
        and approver
        and requester != approver
        and target != requester
        and target != approver
    )


def bootstrap_approval_authorized(
    requester_active, requester_roles, approver_active, approver_roles
):
    return bool(
        requester_active
        and "admin" in requester_roles
        and approver_active
        and "approver" in approver_roles
    )


def verify_operator_password(password, salt, expected):
    actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(actual, expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--roles", help="comma-separated reviewer,viewer,auditor")
    parser.add_argument("--bootstrap-admin", action="store_true", help="operator-only second-admin bootstrap with two-person audit")
    parser.add_argument("--approved-by", help="second active approver who independently approves the exact bootstrap")
    parser.add_argument("--operator", required=True, help="operator identity for the audit event")
    args = parser.parse_args()
    if not USERNAME.fullmatch(args.username):
        parser.error("username format is invalid")
    if args.bootstrap_admin:
        if args.roles or not validate_bootstrap_approval(
            args.operator, args.approved_by, args.username
        ):
            parser.error("bootstrap-admin requires distinct requester/approver/new target identities; do not pass --roles")
        roles = ["admin"]
    else:
        if args.approved_by or not args.roles:
            parser.error("--roles is required; --approved-by is only for --bootstrap-admin")
        roles = sorted(set(item.strip() for item in args.roles.split(",") if item.strip()))
        if not roles or not set(roles) <= PROVISIONABLE_ROLES:
            parser.error("initial roles may only be reviewer, viewer, or auditor")
    requester_password = getpass.getpass("Authenticate requesting admin: ") if args.bootstrap_admin else None
    approver_password = (
        getpass.getpass(
            f"Approve admin role for {args.username}, requested by {args.operator}; authenticate as approver {args.approved_by}: "
        )
        if args.bootstrap_admin else None
    )
    password = getpass.getpass("New account password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if len(password) < 14 or password != confirmation:
        print("Password confirmation failed or password is too short.", file=sys.stderr)
        return 2
    salt = secrets.token_urlsafe(32)
    password_hash = hashlib.scrypt(
        password.encode(), salt=salt.encode(), n=16384, r=8, p=1
    ).hex()
    # Remove references before touching the DB so exceptions cannot accidentally
    # flow password values into later diagnostics.
    del password, confirmation
    try:
        operator_dsn = os.environ["FOLIO_OPERATOR_DSN"]
        with psycopg.connect(operator_dsn) as db:
            access_table = db.execute("SELECT to_regclass('public.folio_workspace_access')").fetchone()[0]
            if access_table is None:
                print("Access schema is not installed; no account was provisioned.", file=sys.stderr)
                return 4
            if not db.execute(
                "SELECT settings_version FROM folio_workspace_access WHERE workspace=%s FOR UPDATE",
                (WORKSPACE,),
            ).fetchone():
                print("Access workspace is not initialized; no account was provisioned.", file=sys.stderr)
                return 4
            if db.execute("SELECT 1 FROM users WHERE username=%s", (args.username,)).fetchone():
                print("Account already exists; no change made.", file=sys.stderr)
                return 3
            if args.bootstrap_admin:
                requester = db.execute(
                    "SELECT salt,password_hash,active,roles FROM users WHERE username=%s FOR SHARE",
                    (args.operator,),
                ).fetchone()
                approver = db.execute(
                    "SELECT salt,password_hash,active,roles FROM users WHERE username=%s FOR SHARE",
                    (args.approved_by,),
                ).fetchone()
                active_admins = db.execute(
                    "SELECT count(*) FROM users WHERE active AND roles ? 'admin'"
                ).fetchone()[0]
                roles_authorized = bool(
                    requester and approver and bootstrap_approval_authorized(
                        requester[2], requester[3], approver[2], approver[3]
                    )
                )
                requester_valid = bool(
                    requester and requester[2] and "admin" in requester[3]
                    and verify_operator_password(requester_password, requester[0], requester[1])
                )
                approver_valid = bool(
                    approver and approver[2] and "approver" in approver[3]
                    and verify_operator_password(approver_password, approver[0], approver[1])
                )
                if not roles_authorized or not requester_valid or not approver_valid or active_admins < 1:
                    print("Bootstrap precondition failed; no account was provisioned.", file=sys.stderr)
                    return 4
            del requester_password, approver_password
            db.execute(
                "INSERT INTO users(username,salt,password_hash,roles,active) VALUES(%s,%s,%s,%s,true)",
                (args.username, salt, password_hash, Jsonb(roles)),
            )
            db.execute(
                "INSERT INTO events(workspace,case_id,kind,actor,body) VALUES(%s,NULL,'DOC-account-provisioned',%s,%s)",
                (WORKSPACE, args.operator, Jsonb({
                    "username": args.username,
                    "roles": roles,
                    "provisioning": "two-person-admin-bootstrap" if args.bootstrap_admin else "operator-cli",
                    **({"approved_by": args.approved_by, "approval_method": "second-authenticated-active-approver", "approved_role": "approver"} if args.bootstrap_admin else {}),
                })),
            )
            db.execute(
                "UPDATE folio_workspace_access SET settings_version=settings_version+1,auth_epoch=auth_epoch+1 WHERE workspace=%s",
                (WORKSPACE,),
            )
    except Exception:
        print("Provisioning failed; inspect the secured operator-side service logs.", file=sys.stderr)
        return 4
    print("Account provisioned. No credential was written to output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
