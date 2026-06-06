from __future__ import annotations

import pytest

from web import config


def test_permission_mode_is_workspace_write():
    assert config.WEB_PERMISSION_MODE == "workspace-write"


def test_auth_secret_reads_env(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_SECRET", "abc123")
    assert config.auth_secret() == "abc123"


def test_auth_secret_dev_fallback_is_stable(monkeypatch):
    monkeypatch.delenv("WEB_AUTH_SECRET", raising=False)
    s1 = config.auth_secret()
    s2 = config.auth_secret()
    assert s1 and s1 == s2  # ephemeral but stable within a process


def test_signup_code_blank_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("WEB_SIGNUP_CODE", raising=False)
    assert config.signup_code() == ""


def test_signup_code_reads_file_when_env_unset(monkeypatch, tmp_path):
    # The toggle persists the gate on disk; the server must pick it up even
    # when the env var never propagated to it (already-running process).
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("WEB_SIGNUP_CODE", raising=False)
    code_file = tmp_path / "web" / "signup_code"
    code_file.parent.mkdir(parents=True)
    code_file.write_text("  letmein\n", encoding="utf-8")
    assert config.signup_code() == "letmein"


def test_signup_code_env_overrides_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_SIGNUP_CODE", "from-env")
    code_file = tmp_path / "web" / "signup_code"
    code_file.parent.mkdir(parents=True)
    code_file.write_text("from-file", encoding="utf-8")
    assert config.signup_code() == "from-env"


def test_rate_limit_default_and_override(monkeypatch):
    monkeypatch.delenv("WEB_RATE_LIMIT_PER_MIN", raising=False)
    assert config.rate_limit_per_min() == 20
    monkeypatch.setenv("WEB_RATE_LIMIT_PER_MIN", "5")
    assert config.rate_limit_per_min() == 5
    monkeypatch.setenv("WEB_RATE_LIMIT_PER_MIN", "garbage")
    assert config.rate_limit_per_min() == 20


def test_pool_knobs_defaults_and_overrides(monkeypatch):
    import importlib

    from web import config
    monkeypatch.delenv("WEB_POOL_ENABLED", raising=False)
    monkeypatch.delenv("WEB_POOL_MAX_HOSTS", raising=False)
    monkeypatch.delenv("WEB_POOL_IDLE_TTL", raising=False)
    importlib.reload(config)
    # Defaults: pool OFF (reversible rollout), sane bounds.
    assert config.pool_enabled() is False
    assert config.pool_max_hosts() == 8
    assert config.pool_idle_ttl() == 600.0

    monkeypatch.setenv("WEB_POOL_ENABLED", "1")
    monkeypatch.setenv("WEB_POOL_MAX_HOSTS", "3")
    monkeypatch.setenv("WEB_POOL_IDLE_TTL", "30")
    assert config.pool_enabled() is True
    assert config.pool_max_hosts() == 3
    assert config.pool_idle_ttl() == 30.0


def test_pool_knobs_bad_values_fall_back(monkeypatch):
    from web import config
    monkeypatch.setenv("WEB_POOL_MAX_HOSTS", "nope")
    monkeypatch.setenv("WEB_POOL_IDLE_TTL", "nope")
    assert config.pool_max_hosts() == 8
    assert config.pool_idle_ttl() == 600.0
