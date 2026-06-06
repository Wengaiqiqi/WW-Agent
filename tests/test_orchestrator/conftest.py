"""Shared fixtures for orchestrator tests.

Every test points ``LANGCHAIN_AGENT_CONFIG_DIR`` at a fresh per-test tempdir so
the real ``.langchain-agent/`` is never touched and persisted state (e.g.
``comm_session.json`` written by ``/comm use``) can't leak between tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path / "config"))
