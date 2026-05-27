from __future__ import annotations

import web.models as models


def test_lists_only_providers_with_credentials(monkeypatch):
    fake_providers = {
        "anthropic": {
            "label": "Anthropic", "api_key_env": "ANTHROPIC_API_KEY",
            "models": ["claude-opus-4-7", "claude-sonnet-4-6"],
        },
        "openai": {
            "label": "OpenAI", "api_key_env": "OPENAI_API_KEY",
            "models": ["gpt-5.4"],
        },
    }
    monkeypatch.setattr(models, "_providers", lambda: fake_providers)
    monkeypatch.setattr(models, "_credentials", lambda: {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    out = models.available_models()
    ids = [m["id"] for m in out]
    # anthropic has a key in env -> included; openai has none -> excluded
    assert "anthropic/claude-opus-4-7" in ids
    assert "anthropic/claude-sonnet-4-6" in ids
    assert all(not i.startswith("openai/") for i in ids)
    assert out[0]["label"] == "Anthropic"


def test_credentials_file_counts_as_configured(monkeypatch):
    fake_providers = {
        "openai": {"label": "OpenAI", "api_key_env": "OPENAI_API_KEY", "models": ["gpt-5.4"]},
    }
    monkeypatch.setattr(models, "_providers", lambda: fake_providers)
    monkeypatch.setattr(models, "_credentials", lambda: {"OPENAI_API_KEY": "sk-y"})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert [m["id"] for m in models.available_models()] == ["openai/gpt-5.4"]
