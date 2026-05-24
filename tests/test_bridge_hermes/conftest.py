"""Fixtures for the Hermes bridge tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture
def fake_acp_argv() -> list[str]:
    """argv list that launches the fake `hermes acp` stub with the test python.

    Returned as a list (not a string) so paths containing spaces — e.g.
    'D:\\Claude Code\\agent-last' — never go through shlex.split.
    """
    stub = Path(__file__).parent / "fake_hermes_acp.py"
    return [sys.executable, str(stub)]
