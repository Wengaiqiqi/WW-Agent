"""Tests for tool/tool_web.py SSRF protection.

A prompt-injected agent asked to "verify by fetching http://127.0.0.1/...",
"check the metadata service at 169.254.169.254", or "look at our internal
10.0.0.5 dashboard" must be refused. The block is enforced at hostname-resolve
time before any socket is opened.
"""
from __future__ import annotations

import gzip
import socket

import pytest

from tool.tool_web import (
    SafeRedirectHandler,
    _MAX_BYTES,
    _classify_fetch_failure,
    _gunzip_capped,
    hostname_is_safe,
    web_extract,
    web_search,
)


def test_gunzip_capped_truncates_decompression_bomb():
    """A tiny gzip payload that expands far past the cap must be truncated,
    not fully inflated into memory (SSRF/zip-bomb DoS guard)."""
    # 50 MB of zeros compresses to a few KB but would blow past _MAX_BYTES.
    bomb = gzip.compress(b"\x00" * (50 * 1024 * 1024))
    assert len(bomb) < _MAX_BYTES  # the compressed payload itself is small
    out = _gunzip_capped(bomb, _MAX_BYTES)
    assert len(out) <= _MAX_BYTES


def test_gunzip_capped_roundtrips_small_payload():
    original = "héllo 世界".encode("utf-8") * 100
    out = _gunzip_capped(gzip.compress(original), _MAX_BYTES)
    assert out == original


def test_blocks_loopback_ip_literal():
    allowed, reason = hostname_is_safe("127.0.0.1")
    assert not allowed
    assert "loopback" in reason or "private" in reason


def test_blocks_ipv6_loopback():
    allowed, reason = hostname_is_safe("::1")
    assert not allowed


def test_blocks_rfc1918_literals():
    for ip in ("10.0.0.5", "172.16.0.1", "192.168.1.1"):
        allowed, reason = hostname_is_safe(ip)
        assert not allowed, f"{ip} should be blocked, got: {reason}"


def test_blocks_link_local_metadata():
    """AWS / GCP / Azure metadata service lives at 169.254.169.254. Critical."""
    allowed, reason = hostname_is_safe("169.254.169.254")
    assert not allowed


def test_blocks_localhost_hostname(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("127.0.0.1", 0))],
    )
    allowed, _ = hostname_is_safe("localhost")
    assert not allowed


def test_blocks_public_hostname_that_resolves_private(monkeypatch):
    """DNS-rebinding-style: attacker-controlled domain that resolves to RFC1918."""
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("10.1.2.3", 0))],
    )
    allowed, reason = hostname_is_safe("evil.example.com")
    assert not allowed
    assert "10.1.2.3" in reason


def test_allows_public_ip(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("8.8.8.8", 0))],
    )
    allowed, _ = hostname_is_safe("dns.google")
    assert allowed


def test_env_opt_out(monkeypatch):
    """Dev escape hatch — local users need to test against localhost dev servers."""
    monkeypatch.setenv("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS", "1")
    allowed, _ = hostname_is_safe("127.0.0.1")
    assert allowed


def test_web_extract_refuses_loopback_url():
    result = web_extract("http://127.0.0.1:11434/api/tags")
    assert result["success"] is False
    assert "SSRF" in result["error"] or "private" in result["error"].lower() \
        or "loopback" in result["error"].lower()


def test_web_extract_refuses_metadata_url():
    result = web_extract("http://169.254.169.254/latest/meta-data/")
    assert result["success"] is False


def test_web_extract_keeps_protocol_check():
    result = web_extract("file:///etc/passwd")
    assert result["success"] is False
    assert "http://" in result["error"]


def test_web_extract_empty_url():
    result = web_extract("")
    assert result["success"] is False


# ---------------------------------------------------------------------------
# Failure classification (retryable vs not) — keeps the LLM loop from probing
# URL/provider variants after a wall it can't get past.
# ---------------------------------------------------------------------------
def test_classify_redirect_loop_not_retryable():
    """Anti-scraping shows up as a 302 redirect loop. Re-fetching variants of
    the same URL hits the same wall — must be flagged non-retryable."""
    import urllib.error

    exc = urllib.error.HTTPError(
        "https://www.luogu.com.cn/problem/P1031", 302,
        "The HTTP server returned a redirect error that would lead to an "
        "infinite loop.\nThe last 30x error message was:\nMoved Temporarily",
        {}, None,
    )
    retryable, advice = _classify_fetch_failure(exc)
    assert retryable is False
    assert advice  # non-empty guidance for the LLM


def test_classify_404_not_retryable():
    import urllib.error

    exc = urllib.error.HTTPError("https://x/gone", 404, "Not Found", {}, None)
    retryable, _ = _classify_fetch_failure(exc)
    assert retryable is False


def test_classify_429_is_retryable():
    import urllib.error

    exc = urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None)
    retryable, _ = _classify_fetch_failure(exc)
    assert retryable is True


def test_classify_5xx_is_retryable():
    import urllib.error

    exc = urllib.error.HTTPError("https://x", 503, "Service Unavailable", {}, None)
    retryable, _ = _classify_fetch_failure(exc)
    assert retryable is True


def test_classify_winerror_10060_not_retryable():
    """The real-world DDG failure: WinError 10060 connection timeout, wrapped
    in a RuntimeError by _search_duckduckgo (so .code is gone — classify on the
    message)."""
    exc = RuntimeError(
        "DuckDuckGo request failed: <urlopen error [WinError 10060] "
        "connection attempt failed>"
    )
    retryable, _ = _classify_fetch_failure(exc)
    assert retryable is False


def test_classify_dns_failure_not_retryable():
    exc = socket.gaierror(11001, "getaddrinfo failed")
    retryable, _ = _classify_fetch_failure(exc)
    assert retryable is False


def test_classify_unknown_defaults_retryable():
    """Don't suppress a retry we can't reason about — only known walls are
    flagged non-retryable."""
    retryable, _ = _classify_fetch_failure(ValueError("something odd"))
    assert retryable is True


def test_web_extract_fetch_failure_carries_classification(monkeypatch):
    """End-to-end: a redirect-loop fetch surfaces retryable=False + advice."""
    import urllib.error
    import tool.tool_web as tw

    def boom(*a, **kw):
        raise urllib.error.HTTPError(
            "https://blocked.example", 302,
            "redirect error that would lead to an infinite loop", {}, None,
        )

    monkeypatch.setattr(tw, "_http_get", boom)
    # bypass DNS / SSRF gate for a public-looking host
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("8.8.8.8", 0))],
    )
    result = web_extract("https://blocked.example/page")
    assert result["success"] is False
    assert result["retryable"] is False
    assert result["advice"]


def test_web_search_failure_carries_classification(monkeypatch):
    """End-to-end: a 10060 timeout from the DDG path surfaces retryable=False."""
    import tool.tool_web as tw

    def boom(*a, **kw):
        raise RuntimeError(
            "DuckDuckGo request failed: <urlopen error [WinError 10060]>"
        )

    monkeypatch.setattr(tw, "_search_duckduckgo", boom)
    result = web_search("anything", provider="duckduckgo")
    assert result["success"] is False
    assert result["retryable"] is False
    assert result["advice"]


def test_web_extract_ssrf_refusal_not_retryable():
    """A blocked private URL is non-retryable — variants won't unblock it."""
    result = web_extract("http://127.0.0.1:11434/api/tags")
    assert result["success"] is False
    assert result["retryable"] is False


def test_safe_redirect_handler_blocks_private_target(monkeypatch):
    """Public host 302s to 127.0.0.1: the redirect handler must refuse.

    Without ``SafeRedirectHandler``, ``urllib.request.urlopen`` would follow
    the redirect transparently and the agent would receive private-network
    content even though the seed host passed ``hostname_is_safe``.
    """
    import io
    import urllib.error

    handler = SafeRedirectHandler()
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **kw: [(socket.AF_INET, None, None, "", ("127.0.0.1", 0))]
        if host == "intra.example.invalid"
        else [(socket.AF_INET, None, None, "", ("8.8.8.8", 0))],
    )
    req = urllib.error.HTTPError(
        "http://public.example.com/", 302, "Found", {}, io.BytesIO(),
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        handler.redirect_request(
            req, io.BytesIO(), 302, "Found", {},
            "http://intra.example.invalid/admin",
        )
    assert "Refused redirect" in str(exc_info.value)


def test_safe_redirect_handler_allows_public_target(monkeypatch):
    """Public→public redirect must still work — only private targets are blocked."""
    import io
    import urllib.error
    import urllib.request

    handler = SafeRedirectHandler()
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("8.8.8.8", 0))],
    )
    req = urllib.request.Request("http://public.example.com/")
    new_req = handler.redirect_request(
        req, io.BytesIO(), 302, "Found", {},
        "http://elsewhere.example.com/landing",
    )
    assert new_req is not None
    assert new_req.full_url == "http://elsewhere.example.com/landing"
