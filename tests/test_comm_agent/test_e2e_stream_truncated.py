"""Server kills connection mid-stream → client yields final error event, no crash."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from agents.comm_agent.a2a_protocol import A2AClient
from agents.comm_agent.peer_registry import Peer

from .conftest import running_peer


@pytest.mark.asyncio
async def test_stream_truncated(tls_ca) -> None:
    async def truncating_stream(skill: str, params: dict, claims: dict) -> AsyncIterator[dict]:
        yield {"type": "task", "state": "working"}
        # Cancel the generator mid-stream (simulates connection drop).
        raise asyncio.CancelledError("simulated drop")

    async with running_peer(
        tls_ca, my_peer_id="remote", hmac_secret="s",
        stream_dispatcher=truncating_stream,
    ) as peer:
        client = A2AClient(
            Peer(
                peer_id="remote", display_name="R", url=peer.base_url,
                hmac_secret_ref="_", tls_verify=False,
                tls_pinned_sha256=peer.fingerprint_sha256,
                added_at="", last_seen=None,
            ),
            secret="s", my_peer_id="caller",
        )
        events = [e async for e in client.stream(method="message/stream", params={}, skill="message/stream")]
        # Got at least the "working" event; final event is an error marker.
        assert events[0]["type"] == "task"
        # Truncation or stream-error event present at end.
        assert events[-1]["type"] == "error"
