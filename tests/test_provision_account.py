import hashlib
from pathlib import Path
from runpy import run_path

validate_bootstrap_approval = run_path(
    Path(__file__).parents[1] / "scripts" / "provision_account.py"
)["validate_bootstrap_approval"]
bootstrap_approval_authorized = run_path(
    Path(__file__).parents[1] / "scripts" / "provision_account.py"
)["bootstrap_approval_authorized"]
verify_operator_password = run_path(
    Path(__file__).parents[1] / "scripts" / "provision_account.py"
)["verify_operator_password"]


def test_admin_bootstrap_requires_distinct_requester_reviewer_and_target():
    assert validate_bootstrap_approval(
        "existing-admin", "seed-approver", "new-admin"
    )
    assert not validate_bootstrap_approval(
        "existing-admin", "existing-admin", "new-admin"
    )
    assert not validate_bootstrap_approval(
        "existing-admin", "seed-approver", "existing-admin"
    )
    assert not validate_bootstrap_approval(
        "existing-admin", "seed-approver", "seed-approver"
    )


def test_seeded_sole_admin_and_separate_approver_can_bootstrap_second_admin():
    assert bootstrap_approval_authorized(True, ["admin"], True, ["approver"])
    assert not bootstrap_approval_authorized(True, ["admin"], False, ["approver"])
    assert not bootstrap_approval_authorized(True, ["admin"], True, ["reviewer"])
    assert not bootstrap_approval_authorized(False, ["admin"], True, ["approver"])


def test_bootstrap_authentication_checks_the_active_account_password_hash():
    salt = "test-salt"
    password_hash = hashlib.scrypt(b"test-password", salt=salt.encode(), n=16384, r=8, p=1).hex()
    assert verify_operator_password("test-password", salt, password_hash)
    assert not verify_operator_password("different-password", salt, password_hash)
