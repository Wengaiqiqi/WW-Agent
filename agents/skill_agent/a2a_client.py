"""Outbound A2A calls from skill-agent to peer specialists (e.g., tool-agent)."""
from __future__ import annotations
import json
from pathlib import Path
import httpx


def _load_peers() -> dict[str, str]:
    p = Path(".agent/runtime/peers.json")
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


async def call_peer(*, peer_id: str, skill_id: str, input: dict, meta: dict) -> dict:
    """Send a tasks/send A2A request to the peer's HTTP endpoint.

    Returns the `result` field of the JSON-RPC response, or raises if the peer
    is unknown or the HTTP call fails.
    """
    peers = _load_peers()
    url = peers.get(peer_id)
    if url is None:
        raise RuntimeError(f"no A2A url known for peer {peer_id!r}")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{url}/a2a", json={
            "jsonrpc": "2.0",
            "id": meta.get("trace_id", "task"),
            "method": "tasks/send",
            "params": {
                "task_id": meta.get("trace_id", "task"),
                "skill_id": skill_id,
                "input": input,
                "_meta": meta,
            },
        })
        resp.raise_for_status()
        return resp.json().get("result", {})
