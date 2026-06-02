"""Fixtures for the security suite — mirrors tests/test_web/conftest.py
(isolated config dir, a test JWT secret, a fresh SQLite db path per test) so
these tests don't depend on the web suite's conftest scope."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
    return tmp_path


@pytest.fixture
def web_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = "test-secret-not-for-production"
    monkeypatch.setenv("WEB_AUTH_SECRET", secret)
    return secret


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "web" / "app.db")
