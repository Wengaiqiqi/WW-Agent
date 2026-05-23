"""Replaying the same grant twice → second call gets 401."""
from __future__ import annotations

import httpx
import pytest

from agents.comm_agent.peer_registry import Peer
from agents.shared.authz import sign_cross_machine_grant

from .conftest import running_peer


@pytest.mark.asyncio
async def test_replay_blocked(tls_ca) -> None:
    async with running_peer(tls_ca, my_peer_id="remote", hmac_secret="s") as peer:
        grant = sign_cross_machine_grant(
            my_peer_id="caller", target_peer_id="remote",
            requested_skill="ping", key="s", ttl_seconds=60,
        )
        body = {
            "jsonrpc": "2.0", "id": "1", "method": "ping",
            "params": {"_meta": {"authz_grant": grant}},
        }
        async with httpx.AsyncClient(verify=False) as c:
            r1 = await c.post(f"{peer.base_url}/a2a", json=body)
            assert r1.status_code == 200
            r2 = await c.post(f"{peer.base_url}/a2a", json=body)
            assert r2.status_code == 401
            assert "replay" in r2.text.lower()
