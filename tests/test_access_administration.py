import pytest
from fastapi import HTTPException

from folio.access_administration import (
    decision_allowed,
    last_admin_after,
    lookup_command_status,
    normalized_roles,
)


def test_approver_role_grant_needs_distinct_admin_and_approver_reviewer():
    assert decision_allowed(
        "admin-requester", "new-approver", "independent-reviewer",
        ["admin", "approver"], "grant_role", "approver",
    )
    assert not decision_allowed(
        "admin-requester", "new-approver", "admin-reviewer",
        ["admin"], "grant_role", "approver",
    )
    assert decision_allowed(
        "admin-requester", "new-approver", "approver-reviewer",
        ["approver"], "grant_role", "approver",
    )
    assert not decision_allowed(
        "admin-requester", "new-approver", "approver-reviewer",
        ["approver"], "grant_role", "admin",
    )
    assert not decision_allowed(
        "admin-requester", "new-approver", "admin-requester",
        ["admin", "approver"], "grant_role", "approver",
    )
    assert not decision_allowed(
        "admin-requester", "admin-reviewer", "admin-reviewer",
        ["admin"], "grant_role", "admin",
    )


def test_last_active_admin_survives_account_changes():
    admins = [
        ("admin-a", True, ["admin"]),
        ("admin-b", True, ["admin", "reviewer"]),
    ]
    assert last_admin_after("admin-a", [], False, admins)
    assert not last_admin_after("admin-a", [], False, admins[:1])
    assert not last_admin_after("admin-a", ["reviewer"], True, admins[:1])
    assert last_admin_after("admin-a", ["admin"], True, admins[:1])


def test_role_normalization_rejects_unknown_or_non_list_values():
    assert normalized_roles(["reviewer", "viewer", "reviewer"]) == ["reviewer", "viewer"]
    for malformed in ("admin", ["root"], [None], {"admin": True}):
        try:
            normalized_roles(malformed)
        except Exception as error:
            assert getattr(error, "detail", None) == "INVALID_ROLE"
        else:
            raise AssertionError("malformed role set was accepted")


def test_receipt_lookup_binds_key_to_exact_hashed_method_and_route():
    class DB:
        def __init__(self, found):
            self.found = found
            self.parameters = None

        def execute(self, sql, parameters):
            self.parameters = parameters
            return self

        def fetchone(self):
            return (1,) if self.found else None

    route_a = "0" * 64
    route_b = "1" * 64
    other_route_receipt = DB(False)
    assert lookup_command_status(other_route_receipt, "workspace", "actor", "key", route_a) == {"status": "not_recorded"}
    assert other_route_receipt.parameters == ("workspace", "actor", route_a, "key")
    exact_receipt = DB(True)
    assert lookup_command_status(exact_receipt, "workspace", "actor", "key", route_b) == {"status": "committed"}


def test_receipt_lookup_rejects_non_sha256_route_binding():
    with pytest.raises(HTTPException) as error:
        lookup_command_status(object(), "workspace", "actor", "key", "/private/raw/path")
    assert error.value.status_code == 404


def test_cookie_secure_policy_allows_local_http_and_requires_https_elsewhere(monkeypatch):
    from folio import workbench

    previous_origin = workbench.ORIGIN
    try:
        monkeypatch.delenv("FOLIO_COOKIE_SECURE", raising=False)
        workbench.ORIGIN = "http://127.0.0.1:5101"
        assert workbench._cookie_secure_setting() is False
        workbench.ORIGIN = "https://folio.example.test"
        assert workbench._cookie_secure_setting() is True
        monkeypatch.setenv("FOLIO_COOKIE_SECURE", "false")
        with pytest.raises(RuntimeError, match="require Secure"):
            workbench._cookie_secure_setting()
        monkeypatch.delenv("FOLIO_COOKIE_SECURE", raising=False)
        workbench.ORIGIN = "http://folio.example.test"
        with pytest.raises(RuntimeError, match="must use HTTPS"):
            workbench._cookie_secure_setting()
    finally:
        workbench.ORIGIN = previous_origin


def test_command_receipt_hash_matches_browser_canonical_method_and_route():
    from folio.workbench import command_route_fingerprint

    route = "/api/v1/workspaces/111/cases/c1/decisions"
    assert command_route_fingerprint("POST", route) == "e877d8f2c25c44b57d6e34d2bbd3ed998b85ac72a72d58772148d5877bb24180"
    assert command_route_fingerprint("POST", route) != command_route_fingerprint("POST", route.replace("c1", "c2"))
    assert command_route_fingerprint("POST", route) != command_route_fingerprint("PUT", route)
