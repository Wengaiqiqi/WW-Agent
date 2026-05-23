"""End-to-end: orchestrator spawns comm-agent, which delegates to a MockA2APeer."""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agents.comm_agent.peer_registry import Peer, PeerRegistry
from tests.test_comm_agent.conftest import cert_fingerprint_sha256, running_peer
import trustme


@pytest.mark.asyncio
async def test_orchestrator_can_drive_comm_delegate(
    tmp_path: Path, monkeypatch,
) -> None:
    """
    Layout:
      - Spin up MockA2APeer (HTTPS via trustme) on 127.0.0.1:<eph>
      - Write a comm_peers.json registering it
      - Build the comm.* MCP tools directly (in-process — skip subprocess spawn
        for this smoke test since the subprocess machinery is already covered
        by test_e2e_spawn_and_handshake.py)
      - Call comm.delegate, verify events come back
    """
    ca = trustme.CA()
    secret = "shared"

    async def stream_dispatcher(skill: str, params: dict, claims: dict) -> AsyncIterator[dict]:
        yield {"type": "task", "state": "working"}
        yield {"type": "task", "state": "completed", "result": "remote-said-hello"}

    async with running_peer(
        ca, my_peer_id="remote", hmac_secret=secret,
        stream_dispatcher=stream_dispatcher,
    ) as peer:
        monkeypatch.setenv("COMM_PEER_REMOTE_HMAC", secret)

        reg = PeerRegistry(tmp_path / "comm_peers.json")
        reg.add(Peer(
            peer_id="remote", display_name="Remote",
            url=peer.base_url,
            hmac_secret_ref="COMM_PEER_REMOTE_HMAC",
            tls_verify=False, tls_pinned_sha256=peer.fingerprint_sha256,
            added_at="", last_seen=None,
        ))

        from agents.comm_agent.mcp_tools import build_comm_tool_specs

        specs = build_comm_tool_specs(reg=reg, my_peer_id="laptop", transport_factory=None)
        by_name = {s.name: s for s in specs}

        out_str = await by_name["comm.delegate"].handler({
            "peer_id": "remote",
            "task": "say hello",
            "stream": False,
        })
        out = json.loads(out_str)
        assert out["ok"] is True
        assert out["final_result"] == "remote-said-hello"
        assert out["events_count"] == 2
