from __future__ import annotations

from config import _credentials, _settings


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


def test_get_api_key_prefers_env_override(monkeypatch):
    from config import make_config

    monkeypatch.setenv("LANGCHAIN_AGENT_API_KEY", "sk-override")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-provider-env")
    cfg = make_config("openai", model="gpt-4o")
    assert _credentials.get_api_key(cfg) == "sk-override"


def test_get_api_key_falls_back_when_no_override(monkeypatch):
    from config import make_config

    monkeypatch.delenv("LANGCHAIN_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-provider-env")
    cfg = make_config("openai", model="gpt-4o")
    assert _credentials.get_api_key(cfg) == "sk-from-provider-env"
