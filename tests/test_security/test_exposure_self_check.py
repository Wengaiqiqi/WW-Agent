from __future__ import annotations

import pytest

from web import config


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_loopback_bind_is_always_safe(host, monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("WEB_AUTH_SECRET", raising=False)
    monkeypatch.delenv("WEB_SIGNUP_CODE", raising=False)
    # Loopback stays zero-config: no raise regardless of secrets.
    config.assert_safe_for_exposure(host)


def test_non_loopback_without_secrets_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("WEB_AUTH_SECRET", raising=False)
    monkeypatch.delenv("WEB_SIGNUP_CODE", raising=False)
    with pytest.raises(config.UnsafeExposureError) as ei:
        config.assert_safe_for_exposure("0.0.0.0")
    msg = str(ei.value)
    assert "WEB_AUTH_SECRET" in msg and "WEB_SIGNUP_CODE" in msg


def test_non_loopback_signup_gate_from_file_counts(monkeypatch, tmp_path):
    # A gate set on disk (via the toggle) must satisfy the exposure check too,
    # not just one set through the env var.
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_AUTH_SECRET", "s3cret-value")
    monkeypatch.delenv("WEB_SIGNUP_CODE", raising=False)
    code_file = tmp_path / "web" / "signup_code"
    code_file.parent.mkdir(parents=True)
    code_file.write_text("gate", encoding="utf-8")
    config.assert_safe_for_exposure("0.0.0.0")  # no raise


def test_non_loopback_with_both_secrets_allowed(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_SECRET", "s3cret-value")
    monkeypatch.setenv("WEB_SIGNUP_CODE", "gate")
    config.assert_safe_for_exposure("0.0.0.0")  # no raise


def test_non_loopback_missing_one_secret_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_AUTH_SECRET", "s3cret-value")
    monkeypatch.delenv("WEB_SIGNUP_CODE", raising=False)
    with pytest.raises(config.UnsafeExposureError) as ei:
        config.assert_safe_for_exposure("192.168.1.10")
    assert "WEB_SIGNUP_CODE" in str(ei.value)
    assert "WEB_AUTH_SECRET" not in str(ei.value)  # only the missing one named
