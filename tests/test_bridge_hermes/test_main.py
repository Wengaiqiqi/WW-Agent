"""Tests for the bridge entrypoint assembly."""
from __future__ import annotations

import httpx
import pytest

from bridge.hermes_a2a.__main__ import build


@pytest.mark.asyncio
async def test_build_serves_agent_card(monkeypatch, fake_acp_argv):
    monkeypatch.setenv("HERMES_A2A_HMAC", "secret-xyz")
    monkeypatch.setenv("HERMES_A2A_MY_PEER_ID", "hermes-home")
    monkeypatch.setenv("HERMES_ACP_CMD", " ".join(fake_acp_argv))  # not started by build()
    app = build()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/.well-known/agent.json")
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "hermes-hermes-home"
    assert card["schemaVersion"] == "0.3"
    assert {s["id"] for s in card["skills"]} == {"task.delegate", "chat.message", "status.query"}


def test_build_requires_hmac(monkeypatch):
    monkeypatch.delenv("HERMES_A2A_HMAC", raising=False)
    with pytest.raises(SystemExit):
        build()
