"""Pure status-to-exit regression for the guarded Folio 008 operator helper."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from round6_apply_008 import exit_code_for_status  # noqa: E402


def test_atomic_apply_success_status_exits_zero():
    assert exit_code_for_status("migration_applied_atomic_assertions_passed") == 0


def test_non_success_status_exits_nonzero():
    assert exit_code_for_status("failed") == 1
