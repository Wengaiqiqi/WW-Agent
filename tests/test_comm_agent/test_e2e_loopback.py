"""End-to-end: A2AClient talking to a real HTTPS uvicorn server via trustme."""
from __future__ import annotations

import pytest

from agents.comm_agent.a2a_protocol import A2AClient
from agents.comm_agent.peer_registry import Peer

from .conftest import running_peer


@pytest.mark.asyncio
async def test_loopback_call(tls_ca) -> None:
    async with running_peer(tls_ca, my_peer_id="remote", hmac_secret="s") as peer:
        client = A2AClient(
            Peer(
                peer_id="remote", display_name="R", url=peer.base_url,
                hmac_secret_ref="_", tls_verify=False,
                tls_pinned_sha256=peer.fingerprint_sha256,
                added_at="", last_seen=None,
            ),
            secret="s", my_peer_id="caller",
        )
        result = await client.call(method="ping", params={"x": 1}, skill="ping")
        assert result["echo"]["x"] == 1


@pytest.mark.asyncio
async def test_loopback_stream(tls_ca) -> None:
    async with running_peer(tls_ca, my_peer_id="remote", hmac_secret="s") as peer:
        client = A2AClient(
            Peer(
                peer_id="remote", display_name="R", url=peer.base_url,
                hmac_secret_ref="_", tls_verify=False,
                tls_pinned_sha256=peer.fingerprint_sha256,
                added_at="", last_seen=None,
            ),
            secret="s", my_peer_id="caller",
        )
        events = [
            e async for e in client.stream(method="message/stream", params={}, skill="task.delegate")
        ]
        assert events[-1]["state"] == "completed"


@pytest.mark.asyncio
async def test_loopback_fetch_card(tls_ca) -> None:
    async with running_peer(tls_ca, my_peer_id="remote") as peer:
        client = A2AClient(
            Peer(
                peer_id="remote", display_name="R", url=peer.base_url,
                hmac_secret_ref="_", tls_verify=False,
                tls_pinned_sha256=peer.fingerprint_sha256,
                added_at="", last_seen=None,
            ),
            secret="s", my_peer_id="caller",
        )
        card = await client.fetch_agent_card()
        assert card["name"] == "remote"
