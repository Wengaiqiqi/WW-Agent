"""Tests for the A2A client half of a2a_protocol.py."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from agents.comm_agent.a2a_protocol import A2AClient, A2AClientError
from agents.comm_agent.peer_registry import Peer


@pytest.fixture
def peer() -> Peer:
    return Peer(
        peer_id="remote",
        display_name="Remote",
        url="https://remote.example:8443",
        hmac_secret_ref="REMOTE_HMAC",
        tls_verify=True,
        tls_pinned_sha256=None,
        added_at="", last_seen=None,
    )


@pytest.mark.asyncio
async def test_call_builds_jsonrpc_envelope(peer: Peer) -> None:
    """A2AClient.call should POST JSON-RPC 2.0 with method + params."""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "1", "result": {"ok": True}},
        )

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="secret", my_peer_id="me", transport=transport)
    result = await client.call(method="ping", params={"foo": "bar"})
    assert result == {"ok": True}
    assert captured["url"] == "https://remote.example:8443/a2a"
    assert captured["body"]["jsonrpc"] == "2.0"
    assert captured["body"]["method"] == "ping"
    assert captured["body"]["params"]["foo"] == "bar"
    assert "_meta" in captured["body"]["params"]
    assert "authz_grant" in captured["body"]["params"]["_meta"]
    assert captured["auth"].startswith("A2A-HMAC ")  # double-write per spec §6.1


@pytest.mark.asyncio
async def test_call_retries_5xx(peer: Peer) -> None:
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {"ok": True}})

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    result = await client.call(method="ping", params={})
    assert result == {"ok": True}
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_call_4xx_no_retry(peer: Peer) -> None:
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, text="bad grant")

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    with pytest.raises(A2AClientError, match="auth refused"):
        await client.call(method="ping", params={})
    assert attempts["n"] == 1  # NOT retried


@pytest.mark.asyncio
async def test_call_5xx_exhausts_retries(peer: Peer) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport, retry_backoff=(0.0, 0.0, 0.0))
    with pytest.raises(A2AClientError, match="retried"):
        await client.call(method="ping", params={})


@pytest.mark.asyncio
async def test_fetch_agent_card(peer: Peer) -> None:
    card_json = {
        "schemaVersion": "0.3",
        "name": "remote",
        "description": "",
        "url": "https://remote.example:8443",
        "version": "1.0",
        "skills": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/.well-known/agent.json")
        return httpx.Response(200, json=card_json)

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    card = await client.fetch_agent_card()
    assert card["name"] == "remote"


@pytest.mark.asyncio
async def test_stream_yields_events_in_order(peer: Peer) -> None:
    """SSE: data: {...}\\n\\n lines decode into a sequence of dicts."""
    sse_body = (
        b'data: {"type":"task","state":"working"}\n\n'
        b'data: {"type":"artifact","name":"x"}\n\n'
        b'data: {"type":"task","state":"completed"}\n\n'
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    events = [e async for e in client.stream(method="message/stream", params={})]
    assert [e["type"] for e in events] == ["task", "artifact", "task"]


@pytest.mark.asyncio
async def test_stream_truncation_yields_error_event(peer: Peer) -> None:
    """If the stream cuts off mid-flight we yield a final 'error' event."""
    async def handler(request: httpx.Request) -> httpx.Response:
        # Half a line — never closes with \\n\\n.
        return httpx.Response(
            200, content=b'data: {"type":"task","state":"working"}\n\ndata: {"incompl',
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    events = [e async for e in client.stream(method="message/stream", params={})]
    # First event survives.
    assert events[0]["type"] == "task"
    # Last event is our truncation signal.
    assert events[-1]["type"] == "error"
    assert "stream truncated" in events[-1]["message"]


@pytest.mark.asyncio
async def test_stream_ignores_blank_and_comment_lines(peer: Peer) -> None:
    sse_body = (
        b': keep-alive comment\n\n'
        b'\n\n'  # blank
        b'data: {"type":"task","state":"completed"}\n\n'
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    events = [e async for e in client.stream(method="message/stream", params={})]
    assert len(events) == 1
    assert events[0]["state"] == "completed"
