from __future__ import annotations

import os
from pathlib import Path

from web import bridge


def test_web_turn_env_sets_and_restores(tmp_config_dir, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_AGENT_PERMISSION_MODE", "read-only")
    monkeypatch.delenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_MODEL", raising=False)

    with bridge._web_turn_env(user_id="u-alice", model_id="anthropic/claude-opus-4-7") as ws:
        assert os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] == "workspace-write"
        assert os.environ["LANGCHAIN_AGENT_MEMORY_USER"] == "u-alice"
        assert os.environ["LANGCHAIN_AGENT_MODEL"] == "anthropic/claude-opus-4-7"
        # per-user workspace under the config dir, and it exists
        assert "u-alice" in os.environ["LANGCHAIN_AGENT_WORKSPACE_ROOT"]
        assert Path(os.environ["LANGCHAIN_AGENT_WORKSPACE_ROOT"]).is_dir()
        assert Path(ws) == Path(os.environ["LANGCHAIN_AGENT_WORKSPACE_ROOT"])

    # restored to the pre-existing value, model var removed (was unset before)
    assert os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] == "read-only"
    assert "LANGCHAIN_AGENT_WORKSPACE_ROOT" not in os.environ
    assert "LANGCHAIN_AGENT_MEMORY_USER" not in os.environ
    assert "LANGCHAIN_AGENT_MODEL" not in os.environ


def test_web_turn_env_two_users_isolated(tmp_config_dir):
    with bridge._web_turn_env(user_id="u-alice", model_id="") as ws_a:
        pass
    with bridge._web_turn_env(user_id="u-bob", model_id="") as ws_b:
        pass
    assert Path(ws_a) != Path(ws_b)
    assert "u-alice" in str(ws_a) and "u-bob" in str(ws_b)
