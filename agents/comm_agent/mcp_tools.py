"""Build MCP ToolSpec list for the comm-agent's stdio surface.

Tools NEVER raise. Errors are returned as JSON ``{"error": "..."}`` so the
calling LLM agent can read and react to them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from agents.comm_agent.a2a_protocol import A2AClient, A2AClientError
from agents.comm_agent.agent_card import AgentCardError, validate_card
from agents.comm_agent.peer_registry import (
    Peer, PeerRegistry, PeerRegistryError,
)
from agents.shared.mcp_server import ToolSpec

log = logging.getLogger(__name__)


TransportFactory = Callable[[], httpx.AsyncBaseTransport | None] | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_var_name_for(peer_id: str) -> str:
    """Derive an env-var name from a peer_id. Same input → same name."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", peer_id).strip("_").upper()
    return f"COMM_PEER_{safe}_HMAC"


def _ok(data: dict) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def _make_client_for(peer: Peer, secret: str, my_peer_id: str, transport=None) -> A2AClient:
    return A2AClient(peer, secret=secret, my_peer_id=my_peer_id, transport=transport)


def build_comm_tool_specs(
    *,
    reg: PeerRegistry,
    my_peer_id: str,
    transport_factory: TransportFactory = None,
) -> list[ToolSpec]:
    """Construct the comm.* tool list.

    ``transport_factory`` is a hook for tests to inject an httpx transport
    that mocks the network. Production passes ``None`` → real network.
    """

    def _transport():
        return transport_factory() if transport_factory else None

    # ---- comm.list_peers ----
    async def list_peers(_args: dict) -> str:
        peers = reg.list_peers()
        return json.dumps({
            "peers": [
                {
                    "peer_id": p.peer_id,
                    "display_name": p.display_name,
                    "url": p.url,
                    "last_seen": p.last_seen,
                }
                for p in peers
            ]
        }, ensure_ascii=False)

    # ---- comm.add_peer ----
    async def add_peer(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        url = args.get("url", "")
        secret_value = args.get("hmac_secret_value", "")
        display_name = args.get("display_name", peer_id)
        if not peer_id or not url or not secret_value:
            return _err("peer_id, url, hmac_secret_value are required")
        env_name = _env_var_name_for(peer_id)
        os.environ[env_name] = secret_value
        peer = Peer(
            peer_id=peer_id,
            display_name=display_name,
            url=url,
            hmac_secret_ref=env_name,
            tls_verify=True,
            tls_pinned_sha256=None,
            added_at=_now_iso(),
            last_seen=None,
        )
        try:
            reg.add(peer)
        except PeerRegistryError as exc:
            return _err(str(exc))
        # Try to fetch agent card — non-fatal if it fails (spec §5: card is soft dep).
        fetched_card: dict | None = None
        try:
            client = _make_client_for(peer, secret_value, my_peer_id, transport=_transport())
            fetched_card = await client.fetch_agent_card()
            try:
                validate_card(fetched_card)
            except AgentCardError as exc:
                log.info("peer %s served a card with issues: %s", peer_id, exc)
            reg.update_last_seen(peer_id, _now_iso())
        except (httpx.HTTPError, A2AClientError) as exc:
            log.info("could not fetch agent card for %s: %s", peer_id, exc)
        return _ok({
            "peer_id": peer_id,
            "env_var_name": env_name,
            "fetched_card": fetched_card,
            "note": f"persist env var: export {env_name}=<value> in your shell profile",
        })

    # ---- comm.remove_peer ----
    async def remove_peer(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        if not peer_id:
            return _err("peer_id is required")
        removed = reg.remove(peer_id)
        return _ok({"peer_id": peer_id, "removed": removed})

    # ---- comm.peer_card ----
    async def peer_card(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        peer = reg.get(peer_id)
        if peer is None:
            return _err(f"unknown peer {peer_id!r}; run comm.add_peer first")
        try:
            secret = reg.resolve_secret(peer)
        except PeerRegistryError as exc:
            return _err(str(exc))
        try:
            client = _make_client_for(peer, secret, my_peer_id, transport=_transport())
            card = await client.fetch_agent_card()
            return json.dumps({"ok": True, "card": card}, ensure_ascii=False)
        except (httpx.HTTPError, A2AClientError) as exc:
            return _err(f"could not fetch card: {exc}")

    specs: list[ToolSpec] = [
        ToolSpec(
            name="comm.list_peers",
            description="List all registered remote A2A peers.",
            input_schema={"type": "object", "properties": {}},
            handler=list_peers,
        ),
        ToolSpec(
            name="comm.add_peer",
            description=(
                "Register a remote A2A peer. The HMAC secret value is stored in "
                "a process env var (name returned in env_var_name); the registry "
                "file holds only the env var name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "peer_id": {"type": "string"},
                    "url": {"type": "string"},
                    "hmac_secret_value": {"type": "string"},
                    "display_name": {"type": "string"},
                },
                "required": ["peer_id", "url", "hmac_secret_value"],
            },
            handler=add_peer,
        ),
        ToolSpec(
            name="comm.remove_peer",
            description="Remove a registered remote A2A peer.",
            input_schema={
                "type": "object",
                "properties": {"peer_id": {"type": "string"}},
                "required": ["peer_id"],
            },
            handler=remove_peer,
        ),
        ToolSpec(
            name="comm.peer_card",
            description="Fetch a remote peer's agent card (live, not cached).",
            input_schema={
                "type": "object",
                "properties": {"peer_id": {"type": "string"}},
                "required": ["peer_id"],
            },
            handler=peer_card,
        ),
    ]
    return specs
