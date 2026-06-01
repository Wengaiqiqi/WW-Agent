from __future__ import annotations

from pathlib import Path

from orchestrator.turn_context import TurnContext


def test_turn_env_emits_only_nonempty_per_turn_vars():
    ctx = TurnContext(
        turn_id="t1", user_id="alice", workspace_root=Path("/ws"),
        permission_mode="workspace-write", model_id="custom/gpt",
        base_url="https://api.x/v1", api_key="sk-1", protocol="openai",
        session_key="conv1", trace_id="tr1", hmac_key="h1",
        runtime_dir=Path("/rt"),
    )
    env = ctx.turn_env()
    assert env["LANGCHAIN_AGENT_MEMORY_USER"] == "alice"
    assert env["LANGCHAIN_AGENT_WORKSPACE_ROOT"] == str(Path("/ws"))
    assert env["LANGCHAIN_AGENT_PERMISSION_MODE"] == "workspace-write"
    assert env["LANGCHAIN_AGENT_MODEL"] == "custom/gpt"
    assert env["LANGCHAIN_AGENT_BASE_URL"] == "https://api.x/v1"
    assert env["LANGCHAIN_AGENT_API_KEY"] == "sk-1"
    assert env["LANGCHAIN_AGENT_PROTOCOL"] == "openai"
    assert env["LANGCHAIN_AGENT_RUNTIME_DIR"] == str(Path("/rt"))


def test_turn_env_omits_empty_optionals():
    ctx = TurnContext(
        turn_id="t2", user_id="", workspace_root=Path("/ws"),
        permission_mode="read-only", model_id="", base_url="", api_key="",
        protocol="", session_key="", trace_id="tr2", hmac_key="h2",
        runtime_dir=Path("/rt2"),
    )
    env = ctx.turn_env()
    # Empty optionals are absent (not set to "") so they don't clobber a child default.
    for absent in ("LANGCHAIN_AGENT_MEMORY_USER", "LANGCHAIN_AGENT_MODEL",
                   "LANGCHAIN_AGENT_BASE_URL", "LANGCHAIN_AGENT_API_KEY",
                   "LANGCHAIN_AGENT_PROTOCOL"):
        assert absent not in env
    # Required-always vars are present.
    assert env["LANGCHAIN_AGENT_PERMISSION_MODE"] == "read-only"
    assert env["LANGCHAIN_AGENT_WORKSPACE_ROOT"] == str(Path("/ws"))


def test_from_env_reads_current_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "bob")
    monkeypatch.setenv("LANGCHAIN_AGENT_MODEL", "deepseek/chat")
    monkeypatch.setenv("LANGCHAIN_AGENT_PERMISSION_MODE", "danger-full-access")
    monkeypatch.delenv("LANGCHAIN_AGENT_BASE_URL", raising=False)
    ctx = TurnContext.from_env(session_key="s", trace_id="tr", hmac_key="h",
                               runtime_dir=tmp_path)
    assert ctx.user_id == "bob"
    assert ctx.model_id == "deepseek/chat"
    assert ctx.permission_mode == "danger-full-access"
    assert ctx.base_url == ""
    assert ctx.turn_id  # auto-generated, non-empty
