"""Shared fixtures for gateway tests.

Every test in this directory points ``LANGCHAIN_AGENT_CONFIG_DIR`` at a
fresh per-test tempdir so:

* the real ``.langchain-agent/`` is never touched;
* sessions / memories / pid files / gateways.json from prior tests don't
  leak in;
* tests can be run in parallel without colliding.

Tests that need a specific helper module (``session_store``, ``tool_memory``,
``credentials``) should import it INSIDE the test so the env var is set
first (most of these modules cache the config dir at function-call time, so
the env-first ordering is safe -- but importing in advance can pin paths if
a future refactor adds module-level caching).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``LANGCHAIN_AGENT_CONFIG_DIR`` at an empty tempdir for the test."""
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    # Also wipe any per-user memory scoping the prior test left behind.
    monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
    return tmp_path
