"""Regression: web specialists must spawn with the SAME LLM config the planner
resolved — not whatever stale ``LANGCHAIN_AGENT_MODEL`` / ``LANGCHAIN_AGENT_API_KEY``
the web server process happened to inherit from its launching shell.

The bug: ``_build_agent_env`` passes those vars through from the server's
os.environ, and ``turn_env`` only overrides them when non-empty. So a web turn
that relied on settings.json (empty per-request model/key) let the tool-agent
subprocess inherit a stale ``openai`` / key ``"x"`` from the shell and 401 —
while the in-process planner correctly used settings.json (deepseek).

Fix: pin the resolved config (provider/model/base_url/api_key/protocol) onto the
TurnContext used to spawn, so ``turn_env`` always carries authoritative values
that override any ambient inheritance.
"""
from __future__ import annotations

from pathlib import Path

from config._providers import ActiveConfig
from orchestrator.turn_context import TurnContext
from web.bridge import _pin_llm_config


def _bare_ctx() -> TurnContext:
    # A turn that did NOT specify a model/key per-request (relies on settings.json).
    return TurnContext(
        turn_id="t", user_id="", workspace_root=Path("."),
        permission_mode="workspace-write", model_id="", base_url="",
        api_key="", protocol="", session_key="", trace_id="t",
        hmac_key="k", runtime_dir=Path(".agent/runtime/web-t"),
    )


def test_pin_populates_turn_env_overriding_empty_request_fields():
    cfg = ActiveConfig(
        provider="deepseek", model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY",
        protocol="openai", api_key="sk-real-key",
    )
    ctx = _pin_llm_config(_bare_ctx(), cfg)
    env = ctx.turn_env()
    # turn_env now carries the resolved config -> overrides the child's ambient
    # LANGCHAIN_AGENT_MODEL / API_KEY inherited from the server's shell.
    assert env["LANGCHAIN_AGENT_MODEL"] == "deepseek/deepseek-v4-flash"
    assert env["LANGCHAIN_AGENT_BASE_URL"] == "https://api.deepseek.com/v1"
    assert env["LANGCHAIN_AGENT_API_KEY"] == "sk-real-key"
    assert env["LANGCHAIN_AGENT_PROTOCOL"] == "openai"


def test_pin_falls_back_to_credentials_for_key(monkeypatch):
    # cfg.api_key empty (settings.json flow): the key must be resolved from the
    # credentials file / env so the specialist can authenticate.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    cfg = ActiveConfig(
        provider="deepseek", model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY",
        protocol="openai", api_key="",
    )
    ctx = _pin_llm_config(_bare_ctx(), cfg)
    assert ctx.turn_env()["LANGCHAIN_AGENT_API_KEY"] == "sk-from-env"


def test_pin_does_not_set_key_when_none_resolvable(monkeypatch):
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    cfg = ActiveConfig(
        provider="custom", model="m", base_url="https://x/v1",
        api_key_env="CUSTOM_API_KEY", protocol="openai", api_key="",
    )
    ctx = _pin_llm_config(_bare_ctx(), cfg)
    # No key anywhere -> don't fabricate one; leave it absent so the specialist
    # falls back to its own credential hydration rather than getting "".
    assert "LANGCHAIN_AGENT_API_KEY" not in ctx.turn_env()
    # Model/base_url/protocol are still pinned.
    assert ctx.turn_env()["LANGCHAIN_AGENT_MODEL"] == "custom/m"
