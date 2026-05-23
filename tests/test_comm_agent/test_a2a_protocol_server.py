"""Tests for the server-side build_app() of a2a_protocol.py."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from agents.comm_agent.a2a_protocol import build_app
from agents.comm_agent.agent_card import build_self_card
from agents.shared.authz import sign_cross_machine_grant


SECRET = "shared"


def _self_card() -> dict:
    return build_self_card(
        name="me", description="d",
        public_url="https://me.test:8443", version="1.0",
    )


async def _noop_sync(skill: str, params: dict, claims: dict) -> dict:
    return {"echo": params}


async def _noop_stream(skill: str, params: dict, claims: dict) -> AsyncIterator[dict]:
    yield {"type": "task", "state": "working"}
    yield {"type": "task", "state": "completed", "result": "ok"}


@pytest.mark.asyncio
async def test_get_agent_card() -> None:
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/.well-known/agent.json")
        assert r.status_code == 200
        assert r.json()["name"] == "me"


@pytest.mark.asyncio
async def test_post_a2a_requires_grant() -> None:
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "1", "method": "ping", "params": {},
        })
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_a2a_with_valid_grant() -> None:
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    grant = sign_cross_machine_grant(
        my_peer_id="caller", target_peer_id="me",
        requested_skill="ping", key=SECRET, ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "1", "method": "ping",
            "params": {"foo": "bar", "_meta": {"authz_grant": grant}},
        })
        assert r.status_code == 200
        envelope = r.json()
        assert envelope["result"]["echo"]["foo"] == "bar"


@pytest.mark.asyncio
async def test_post_a2a_wrong_target_rejected() -> None:
    """Grant says target='other' but the server is 'me' → 401."""
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    grant = sign_cross_machine_grant(
        my_peer_id="caller", target_peer_id="other",
        requested_skill="ping", key=SECRET, ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "1", "method": "ping",
            "params": {"_meta": {"authz_grant": grant}},
        })
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_a2a_replay_rejected() -> None:
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    grant = sign_cross_machine_grant(
        my_peer_id="caller", target_peer_id="me",
        requested_skill="ping", key=SECRET, ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        body = {
            "jsonrpc": "2.0", "id": "1", "method": "ping",
            "params": {"_meta": {"authz_grant": grant}},
        }
        r1 = await c.post("/a2a", json=body)
        assert r1.status_code == 200
        r2 = await c.post("/a2a", json=body)
        assert r2.status_code == 401


@pytest.mark.asyncio
async def test_post_stream_yields_sse() -> None:
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    grant = sign_cross_machine_grant(
        my_peer_id="caller", target_peer_id="me",
        requested_skill="message/stream", key=SECRET, ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with c.stream(
            "POST", "/a2a/stream",
            json={
                "jsonrpc": "2.0", "id": "1", "method": "message/stream",
                "params": {"_meta": {"authz_grant": grant}},
            },
        ) as r:
            assert r.status_code == 200
            body = b"".join([chunk async for chunk in r.aiter_bytes()])
            text = body.decode("utf-8")
            # Two events were yielded by _noop_stream.
            assert text.count("data:") == 2
            assert '"state":"completed"' in text
