"""End-to-end: agent-last's real A2AClient drives the bridge's build_app over an
in-process ASGI transport (no TLS, no real Hermes — CI-friendly). The ACP side
is the fake `hermes acp` stub. Exercises the full grant/auth/SSE path."""
from __future__ import annotations

import httpx
import pytest

from agents.comm_agent.a2a_protocol import A2AClient, build_app
from agents.comm_agent.agent_card import build_self_card
from agents.comm_agent.peer_registry import Peer
from bridge.hermes_a2a.acp_client import HermesACPClient
from bridge.hermes_a2a.dispatchers import make_dispatchers

SECRET = "shared-secret"
PEER_ID = "hermes-home"


def _make_client(app) -> A2AClient:
    peer = Peer(
        peer_id=PEER_ID, display_name="h", url="https://hermes-home",
        hmac_secret_ref="X", tls_verify=True, tls_pinned_sha256=None,
        added_at="", last_seen=None,
    )
    transport = httpx.ASGITransport(app=app)
    return A2AClient(peer, secret=SECRET, my_peer_id="agent-last-laptop", transport=transport)


def _build_app(fake_acp_argv):
    acp = HermesACPClient(argv=fake_acp_argv)
    skill_d, stream_d = make_dispatchers(acp)
    card = build_self_card(name=f"hermes-{PEER_ID}", description="x",
                           public_url="https://hermes-home", version="1.0.0")
    app = build_app(self_card=card, hmac_secret=SECRET, my_peer_id=PEER_ID,
                    skill_dispatcher=skill_d, stream_dispatcher=stream_d)
    return app, acp


@pytest.mark.asyncio
async def test_delegate_end_to_end(fake_acp_argv):
    app, acp = _build_app(fake_acp_argv)
    try:
        client = _make_client(app)
        events = [
            ev async for ev in client.stream(
                method="message/stream",
                params={"message": {"role": "user", "parts": [{"text": "hi"}]}},
                skill="task.delegate",
            )
        ]
    finally:
        await acp.aclose()
    completed = [e for e in events if e.get("type") == "task" and e.get("state") == "completed"]
    assert completed and completed[0]["result"] == "echo: hi"


@pytest.mark.asyncio
async def test_chat_multiturn_end_to_end(fake_acp_argv):
    app, acp = _build_app(fake_acp_argv)
    try:
        client = _make_client(app)
        first = await client.call(
            method="message/send",
            params={"message": {"role": "user", "parts": [{"text": "hello"}]},
                    "context_id": None},
            skill="chat.message",
        )
        assert first["reply"] == "echo: hello"
        ctx = first["context_id"]
        second = await client.call(
            method="message/send",
            params={"message": {"role": "user", "parts": [{"text": "again"}]},
                    "context_id": ctx},
            skill="chat.message",
        )
        assert second["context_id"] == ctx
    finally:
        await acp.aclose()


@pytest.mark.asyncio
async def test_status_end_to_end(fake_acp_argv):
    app, acp = _build_app(fake_acp_argv)
    try:
        client = _make_client(app)
        result = await client.call(method="status/query", params={}, skill="status.query")
    finally:
        await acp.aclose()
    assert result["state"] in {"idle", "working"}


@pytest.mark.asyncio
async def test_bad_secret_is_refused(fake_acp_argv):
    app, acp = _build_app(fake_acp_argv)
    try:
        peer = Peer(peer_id=PEER_ID, display_name="h", url="https://hermes-home",
                    hmac_secret_ref="X", tls_verify=True, tls_pinned_sha256=None,
                    added_at="", last_seen=None)
        bad = A2AClient(peer, secret="WRONG", my_peer_id="agent-last-laptop",
                        transport=httpx.ASGITransport(app=app))
        from agents.comm_agent.a2a_protocol import A2AClientError
        with pytest.raises(A2AClientError):
            await bad.call(method="status/query", params={}, skill="status.query")
    finally:
        await acp.aclose()
