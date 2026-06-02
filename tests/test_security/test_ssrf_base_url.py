from __future__ import annotations

import pytest
from fastapi import HTTPException

from web.app import _assert_safe_base_url


@pytest.mark.parametrize("base_url", [
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
    "http://127.0.0.1:11434/v1",                  # loopback
    "http://localhost/v1",                        # loopback name
    "http://10.0.0.5/v1",                         # private (RFC1918)
    "http://192.168.1.10/v1",                     # private
    "http://172.16.0.1/v1",                       # private
    "http://[::1]/v1",                            # ipv6 loopback
])
def test_private_and_metadata_base_urls_rejected(base_url, monkeypatch):
    # Ensure the dev escape hatch is OFF so the guard is active. These are all
    # literal IPs / loopback names, so no live DNS is needed.
    monkeypatch.delenv("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS", raising=False)
    with pytest.raises(HTTPException) as ei:
        _assert_safe_base_url(base_url)
    assert ei.value.status_code == 400
    assert "not allowed" in ei.value.detail


def test_public_base_url_allowed(monkeypatch):
    # Hermetic: don't depend on live DNS — a resolvable public host is the
    # contract, so stub the resolver to "safe" and assert no raise.
    import tool.tool_web as tw
    monkeypatch.delenv("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(tw, "hostname_is_safe", lambda host: (True, ""))
    _assert_safe_base_url("https://api.openai.com/v1")  # no raise


def test_escape_hatch_allows_private(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS", "1")
    # With the documented dev escape hatch, a localhost endpoint is allowed.
    _assert_safe_base_url("http://127.0.0.1:11434/v1")  # no raise


def test_remote_http_endpoint_rejected_requires_https(monkeypatch):
    # A plaintext http endpoint to a public host leaks the API key and isn't
    # rebinding-protected — must be rejected (use https).
    import tool.tool_web as tw
    monkeypatch.delenv("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(tw, "hostname_is_safe", lambda host: (True, ""))
    with pytest.raises(HTTPException) as ei:
        _assert_safe_base_url("http://api.example.com/v1")
    assert ei.value.status_code == 400
    assert "https" in ei.value.detail


def test_remote_https_endpoint_allowed(monkeypatch):
    import tool.tool_web as tw
    monkeypatch.delenv("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(tw, "hostname_is_safe", lambda host: (True, ""))
    _assert_safe_base_url("https://api.example.com/v1")  # no raise
