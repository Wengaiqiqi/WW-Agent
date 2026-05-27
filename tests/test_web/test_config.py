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


def test_signup_code_blank_by_default(monkeypatch):
    monkeypatch.delenv("WEB_SIGNUP_CODE", raising=False)
    assert config.signup_code() == ""


def test_rate_limit_default_and_override(monkeypatch):
    monkeypatch.delenv("WEB_RATE_LIMIT_PER_MIN", raising=False)
    assert config.rate_limit_per_min() == 20
    monkeypatch.setenv("WEB_RATE_LIMIT_PER_MIN", "5")
    assert config.rate_limit_per_min() == 5
    monkeypatch.setenv("WEB_RATE_LIMIT_PER_MIN", "garbage")
    assert config.rate_limit_per_min() == 20
