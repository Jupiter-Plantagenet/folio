"""Keep test temporaries in the agent-owned scratch, not the system temp tree."""

import os
from pathlib import Path

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if config.option.basetemp is None:
        root = Path(
            os.environ.get(
                "TMPDIR",
                str(Path(__file__).resolve().parents[1] / ".cache/test-scratch"),
            )
        )
        root.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(root / f"folio-pytest-{os.getpid()}")
