"""Tests for the comm.* MCP tools."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from agents.comm_agent.mcp_tools import build_comm_tool_specs
from agents.comm_agent.peer_registry import Peer, PeerRegistry


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
async def test_add_peer_rejects_non_http_scheme(reg) -> None:
    """A url with no http(s) scheme is refused before any env var is written."""
    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=None)
    by_name = {s.name: s for s in specs}
    out = json.loads(await by_name["comm.add_peer"].handler({
        "peer_id": "remote", "url": "ftp://r:21", "hmac_secret_value": "s",
    }))
    assert out["ok"] is False
    assert "http" in out["error"]
    assert reg.get("remote") is None  # nothing persisted


@pytest.mark.asyncio
async def test_add_peer_warns_on_plaintext_http(reg) -> None:
    """http:// is allowed (localhost/VPN) but flagged: the grant is cleartext."""
    async def fake_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "schemaVersion": "0.3", "name": "remote", "description": "",
            "url": "http://127.0.0.1:8001", "version": "1.0", "skills": [],
        })

    specs = build_comm_tool_specs(
        reg=reg, my_peer_id="me", transport_factory=lambda: httpx.MockTransport(fake_handler),
    )
    by_name = {s.name: s for s in specs}
    out = json.loads(await by_name["comm.add_peer"].handler({
        "peer_id": "local", "url": "http://127.0.0.1:8001", "hmac_secret_value": "s",
    }))
    assert out["ok"] is True
    assert out["warnings"] and "cleartext" in out["warnings"][0]
    assert reg.get("local") is not None  # http still registers


@pytest.mark.asyncio
async def test_add_peer_rejects_env_var_collision(reg) -> None:
    """Two peer_ids that map to the same env var (e.g. 'peer-1' / 'peer.1')
    must not silently overwrite each other's secret."""
    async def card_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "schemaVersion": "0.3", "name": "r", "description": "",
            "url": "https://r:8443", "version": "1.0", "skills": [],
        })
    specs = build_comm_tool_specs(
        reg=reg, my_peer_id="me",
        transport_factory=lambda: httpx.MockTransport(card_ok),
    )
    by_name = {s.name: s for s in specs}
    first = json.loads(await by_name["comm.add_peer"].handler({
        "peer_id": "peer-1", "url": "https://r:8443", "hmac_secret_value": "A",
    }))
    assert first["ok"] is True
    second = json.loads(await by_name["comm.add_peer"].handler({
        "peer_id": "peer.1", "url": "https://r:8443", "hmac_secret_value": "B",
    }))
    assert second["ok"] is False
    assert "collides" in second["error"]
    # First peer's secret stays intact
    assert os.environ[first["env_var_name"]] == "A"


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


@pytest.mark.asyncio
async def test_delegate_non_streaming(reg, monkeypatch) -> None:
    """stream=false returns final_result, events_count, duration_ms."""
    async def fake_handler(request: httpx.Request) -> httpx.Response:
        # Three SSE frames then close.
        body = (
            b'data: {"type":"task","state":"working"}\n\n'
            b'data: {"type":"task","state":"completed","result":"42"}\n\n'
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(fake_handler)
    monkeypatch.setenv("COMM_PEER_REMOTE_HMAC", "s")
    reg.add(Peer(
        peer_id="remote", display_name="R", url="https://r:8443",
        hmac_secret_ref="COMM_PEER_REMOTE_HMAC", tls_verify=True,
        tls_pinned_sha256=None, added_at="", last_seen=None,
    ))

    def make_transport():
        return transport

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=make_transport)
    by_name = {s.name: s for s in specs}
    out_str = await by_name["comm.delegate"].handler({
        "peer_id": "remote",
        "task": "do the thing",
        "stream": False,
    })
    out = json.loads(out_str)
    assert out["ok"] is True
    assert out["events_count"] == 2
    assert out["final_result"] == "42"


@pytest.mark.asyncio
async def test_status_returns_remote_state(reg, monkeypatch) -> None:
    monkeypatch.setenv("COMM_PEER_REMOTE_HMAC", "s")
    reg.add(Peer(
        peer_id="remote", display_name="R", url="https://r:8443",
        hmac_secret_ref="COMM_PEER_REMOTE_HMAC", tls_verify=True,
        tls_pinned_sha256=None, added_at="", last_seen=None,
    ))

    async def fake_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "1",
            "result": {"state": "idle", "current_task": None, "last_error": None},
        })

    transport = httpx.MockTransport(fake_handler)

    def make_transport():
        return transport

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=make_transport)
    by_name = {s.name: s for s in specs}
    out_str = await by_name["comm.status"].handler({"peer_id": "remote"})
    out = json.loads(out_str)
    assert out["ok"] is True
    assert out["status"]["state"] == "idle"


@pytest.mark.asyncio
async def test_chat_returns_reply_and_context_id(reg, monkeypatch) -> None:
    monkeypatch.setenv("COMM_PEER_REMOTE_HMAC", "s")
    reg.add(Peer(
        peer_id="remote", display_name="R", url="https://r:8443",
        hmac_secret_ref="COMM_PEER_REMOTE_HMAC", tls_verify=True,
        tls_pinned_sha256=None, added_at="", last_seen=None,
    ))

    async def fake_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "1",
            "result": {"reply": "hi back", "context_id": "ctx-abc"},
        })

    transport = httpx.MockTransport(fake_handler)

    def make_transport():
        return transport

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=make_transport)
    by_name = {s.name: s for s in specs}
    out_str = await by_name["comm.chat"].handler({
        "peer_id": "remote", "message": "hi", "context_id": None,
    })
    out = json.loads(out_str)
    assert out["ok"] is True
    assert out["reply"] == "hi back"
    assert out["context_id"] == "ctx-abc"
