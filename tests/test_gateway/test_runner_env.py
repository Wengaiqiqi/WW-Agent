"""Tests for the shared per-turn env primitives in ``gateway.runner`` that the
gateway and the web bridge both build on (``scoped_env`` / ``private_runtime_dir``).
"""
from __future__ import annotations

import os
from pathlib import Path

from gateway.runner import private_runtime_dir, scoped_env

_K = "LANGCHAIN_AGENT_TEST_SCOPED"


def test_scoped_env_sets_then_restores_previously_unset(monkeypatch):
    monkeypatch.delenv(_K, raising=False)
    with scoped_env({_K: "value"}):
        assert os.environ[_K] == "value"
    assert _K not in os.environ  # restored to unset


def test_scoped_env_restores_previous_value(monkeypatch):
    monkeypatch.setenv(_K, "original")
    with scoped_env({_K: "temporary"}):
        assert os.environ[_K] == "temporary"
    assert os.environ[_K] == "original"


def test_scoped_env_none_clears_for_the_block(monkeypatch):
    monkeypatch.setenv(_K, "original")
    with scoped_env({_K: None}):
        assert _K not in os.environ
    assert os.environ[_K] == "original"


def test_scoped_env_restores_on_exception(monkeypatch):
    monkeypatch.delenv(_K, raising=False)
    try:
        with scoped_env({_K: "value"}):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert _K not in os.environ


def test_private_runtime_dir_sets_env_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.delenv("LANGCHAIN_AGENT_RUNTIME_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    seen: Path | None = None
    with private_runtime_dir("test") as rt:
        seen = rt
        assert os.environ["LANGCHAIN_AGENT_RUNTIME_DIR"] == str(rt)
        rt.mkdir(parents=True, exist_ok=True)
        (rt / "peers.json").write_text("{}", encoding="utf-8")
    # Env restored (unset) and the per-PID dir removed.
    assert "LANGCHAIN_AGENT_RUNTIME_DIR" not in os.environ
    assert seen is not None and not seen.exists()
