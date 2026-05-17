"""Tests for tool/tool_web.py SSRF protection.

A prompt-injected agent asked to "verify by fetching http://127.0.0.1/...",
"check the metadata service at 169.254.169.254", or "look at our internal
10.0.0.5 dashboard" must be refused. The block is enforced at hostname-resolve
time before any socket is opened.
"""
from __future__ import annotations

import socket

import pytest

from tool.tool_web import _hostname_is_safe, web_extract


def test_blocks_loopback_ip_literal():
    allowed, reason = _hostname_is_safe("127.0.0.1")
    assert not allowed
    assert "loopback" in reason or "private" in reason


def test_blocks_ipv6_loopback():
    allowed, reason = _hostname_is_safe("::1")
    assert not allowed


def test_blocks_rfc1918_literals():
    for ip in ("10.0.0.5", "172.16.0.1", "192.168.1.1"):
        allowed, reason = _hostname_is_safe(ip)
        assert not allowed, f"{ip} should be blocked, got: {reason}"


def test_blocks_link_local_metadata():
    """AWS / GCP / Azure metadata service lives at 169.254.169.254. Critical."""
    allowed, reason = _hostname_is_safe("169.254.169.254")
    assert not allowed


def test_blocks_localhost_hostname(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("127.0.0.1", 0))],
    )
    allowed, _ = _hostname_is_safe("localhost")
    assert not allowed


def test_blocks_public_hostname_that_resolves_private(monkeypatch):
    """DNS-rebinding-style: attacker-controlled domain that resolves to RFC1918."""
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("10.1.2.3", 0))],
    )
    allowed, reason = _hostname_is_safe("evil.example.com")
    assert not allowed
    assert "10.1.2.3" in reason


def test_allows_public_ip(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("8.8.8.8", 0))],
    )
    allowed, _ = _hostname_is_safe("dns.google")
    assert allowed


def test_env_opt_out(monkeypatch):
    """Dev escape hatch — local users need to test against localhost dev servers."""
    monkeypatch.setenv("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS", "1")
    allowed, _ = _hostname_is_safe("127.0.0.1")
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
