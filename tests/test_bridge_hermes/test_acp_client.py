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
