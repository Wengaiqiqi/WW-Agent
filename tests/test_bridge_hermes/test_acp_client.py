"""Tests for HermesACPClient against the fake `hermes acp` stub."""
from __future__ import annotations

import pytest

from bridge.hermes_a2a.acp_client import ACPError, HermesACPClient


@pytest.mark.asyncio
async def test_ensure_session_returns_session_id(fake_acp_argv):
    acp = HermesACPClient(argv=fake_acp_argv)
    try:
        sid = await acp.ensure_session(None)
        assert sid == "sess-1"
        # Reusing a known context_id returns the same id (no new session).
        assert await acp.ensure_session(sid) == "sess-1"
        # Unknown context_id allocates a fresh session.
        assert await acp.ensure_session("does-not-exist") == "sess-2"
    finally:
        await acp.aclose()


@pytest.mark.asyncio
async def test_run_prompt_streams_text_then_completes(fake_acp_argv):
    acp = HermesACPClient(argv=fake_acp_argv)
    try:
        sid = await acp.ensure_session(None)
        events = [ev async for ev in acp.run_prompt(sid, "hello world")]
    finally:
        await acp.aclose()

    text_events = [e for e in events if e.get("type") == "text"]
    assert "".join(e["text"] for e in text_events) == "echo: hello world"

    completed = [e for e in events if e.get("type") == "task" and e.get("state") == "completed"]
    assert len(completed) == 1
    assert completed[0]["result"] == "echo: hello world"


@pytest.mark.asyncio
async def test_run_prompt_failure_yields_failed_event(fake_acp_argv, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_FAIL_PROMPT", "1")
    acp = HermesACPClient(argv=fake_acp_argv)
    try:
        sid = await acp.ensure_session(None)
        events = [ev async for ev in acp.run_prompt(sid, "boom")]
    finally:
        await acp.aclose()
    assert any(e.get("type") == "task" and e.get("state") == "failed" for e in events)
    assert not any(e.get("state") == "completed" for e in events)
