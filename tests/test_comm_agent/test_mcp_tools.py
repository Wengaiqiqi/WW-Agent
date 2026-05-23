"""Tests for the comm.* MCP tools."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from agents.comm_agent.mcp_tools import build_comm_tool_specs
from agents.comm_agent.peer_registry import PeerRegistry


@pytest.fixture
def reg(tmp_path: Path) -> PeerRegistry:
    return PeerRegistry(tmp_path / "comm_peers.json")


@pytest.mark.asyncio
async def test_list_peers_empty(reg) -> None:
    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=None)
    by_name = {s.name: s for s in specs}
    out = await by_name["comm.list_peers"].handler({})
    assert json.loads(out) == {"peers": []}


@pytest.mark.asyncio
async def test_add_peer_writes_env_var_and_card_fetch(
    reg, monkeypatch
) -> None:
    """add_peer stores secret in os.environ + persists ref + fetches card."""
    async def fake_handler(request: httpx.Request) -> httpx.Response:
        # Mock /.well-known/agent.json
        return httpx.Response(200, json={
            "schemaVersion": "0.3",
            "name": "remote", "description": "", "url": "https://r:8443",
            "version": "1.0", "skills": [],
        })

    transport = httpx.MockTransport(fake_handler)

    def make_transport():
        return transport

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=make_transport)
    by_name = {s.name: s for s in specs}
    out_str = await by_name["comm.add_peer"].handler({
        "peer_id": "remote",
        "url": "https://r:8443",
        "hmac_secret_value": "supersecret",
        "display_name": "Remote",
    })
    out = json.loads(out_str)
    assert out["ok"] is True
    assert out["env_var_name"]  # tool tells caller which env var was set
    env_name = out["env_var_name"]
    assert os.environ[env_name] == "supersecret"
    # Registry has the ref, NOT the value
    peer = reg.get("remote")
    assert peer is not None
    assert peer.hmac_secret_ref == env_name


@pytest.mark.asyncio
async def test_remove_peer(reg, monkeypatch) -> None:
    async def fake_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "schemaVersion": "0.3", "name": "r", "description": "",
            "url": "https://r:8443", "version": "1.0", "skills": [],
        })

    transport = httpx.MockTransport(fake_handler)
    def make_transport():
        return transport

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=make_transport)
    by_name = {s.name: s for s in specs}
    await by_name["comm.add_peer"].handler({
        "peer_id": "remote", "url": "https://r:8443", "hmac_secret_value": "s",
    })
    out_str = await by_name["comm.remove_peer"].handler({"peer_id": "remote"})
    assert json.loads(out_str)["ok"] is True
    assert reg.get("remote") is None


@pytest.mark.asyncio
async def test_unknown_peer_returns_error_not_exception(reg) -> None:
    """comm.* tools must NEVER raise — error returned in payload."""
    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=None)
    by_name = {s.name: s for s in specs}
    out_str = await by_name["comm.peer_card"].handler({"peer_id": "nope"})
    out = json.loads(out_str)
    assert "error" in out
    assert "unknown peer" in out["error"]
