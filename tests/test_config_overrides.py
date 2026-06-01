from __future__ import annotations

from pathlib import Path

from config import _credentials, _settings
from orchestrator.turn_context import TurnContext


def _ctx(**over):
    base = dict(turn_id="t", user_id="", workspace_root=Path("/ws"),
                permission_mode="workspace-write", model_id="", base_url="",
                api_key="", protocol="", session_key="", trace_id="tr",
                hmac_key="h", runtime_dir=Path("/rt"))
    base.update(over)
    return TurnContext(**base)


def test_resolve_config_applies_ctx_overrides():
    cfg = _settings.resolve_config(_ctx(
        model_id="deepseek/deepseek-chat", base_url="https://api.x/v1",
        api_key="sk-ctx", protocol="openai",
    ))
    assert cfg.base_url == "https://api.x/v1"
    assert cfg.api_key == "sk-ctx"
    assert cfg.protocol == "openai"


def test_resolve_config_rejects_unknown_protocol():
    cfg = _settings.resolve_config(_ctx(
        model_id="deepseek/deepseek-chat", protocol="gemini",
    ))
    # Unknown protocol is ignored (kept as resolved default), per the whitelist.
    assert cfg.protocol != "gemini"


def test_base_url_and_protocol_overrides_applied(monkeypatch):
    # "custom" provider exists in the registry (base_url "", protocol openai).
    monkeypatch.setenv("LANGCHAIN_AGENT_MODEL", "custom/gpt-5.4")
    monkeypatch.setenv("LANGCHAIN_AGENT_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("LANGCHAIN_AGENT_PROTOCOL", "anthropic")
    cfg = _settings.load_active_config()
    assert cfg.provider == "custom"
    assert cfg.model == "gpt-5.4"
    assert cfg.base_url == "https://example.test/v1"
    assert cfg.protocol == "anthropic"


def test_no_overrides_leaves_registry_defaults(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_AGENT_MODEL", "openai/gpt-4o")
    monkeypatch.delenv("LANGCHAIN_AGENT_BASE_URL", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_PROTOCOL", raising=False)
    cfg = _settings.load_active_config()
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.protocol == "openai"


def test_get_api_key_prefers_cfg_api_key(monkeypatch):
    # New contract: the per-turn key lives on cfg.api_key (set from the
    # TurnContext), not the LANGCHAIN_AGENT_API_KEY env var. The cfg key wins and
    # the env override is no longer consulted, so a parallel turn can't pick up
    # another turn's key off process-global env.
    from config import make_config

    monkeypatch.setenv("LANGCHAIN_AGENT_API_KEY", "sk-env-should-be-ignored")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-provider-env")
    cfg = make_config("openai", model="gpt-4o")
    cfg.api_key = "sk-ctx"  # ActiveConfig is a non-frozen dataclass
    assert _credentials.get_api_key(cfg) == "sk-ctx"


def test_get_api_key_falls_back_to_provider_env_when_no_cfg_key(monkeypatch):
    from config import make_config

    monkeypatch.delenv("LANGCHAIN_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-provider-env")
    cfg = make_config("openai", model="gpt-4o")  # cfg.api_key defaults to ""
    assert _credentials.get_api_key(cfg) == "sk-from-provider-env"
