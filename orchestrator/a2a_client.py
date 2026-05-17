"""Outbound A2A client for the orchestrator.

Provides streaming SSE consumption for agent-level task delegation,
plus a non-streaming helper for backward-compatible RPC calls.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger(__name__)


def _load_peers() -> dict[str, str]:
    """Read {agent_id: url} from the runtime peers file written by the orchestrator."""
    peers_file = Path(".agent/runtime/peers.json")
    if not peers_file.exists():
        raise RuntimeError(f"peers file not found: {peers_file}")
    return json.loads(peers_file.read_text(encoding="utf-8"))


async def delegate_task(
    *, peer_id: str, task: str, meta: dict, context: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """Send a task to a peer agent's /a2a/stream endpoint and yield SSE events.

    Yields event dicts: thinking, tool_call, tool_result, text, done, error.
    """
    peers = _load_peers()
    url = peers.get(peer_id)
    if not url:
        raise RuntimeError(f"unknown peer: {peer_id}")

    trace_id = meta.get("trace_id", "task")

    # Long-running agent tasks may include tool calls (pip install, docx
    # extraction, model thinking) that each exceed 60s. Use a per-read timeout
    # rather than a total, so we don't kill a task that is making steady
    # progress but happens to take >5 minutes overall.
    #
    # trust_env=False: A2A is always a 127.0.0.1 → 127.0.0.1 call between two
    # local agent processes. If the user's shell has HTTP_PROXY pointing at a
    # local proxy like Clash/V2Ray (`http://127.0.0.1:7890`), httpx will try to
    # route the call through that proxy, which deadlocks because the proxy
    # cannot forward a request back to localhost. Bypass env entirely.
    http_timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=http_timeout, trust_env=False) as client:
        async with client.stream(
            "POST",
            f"{url}/a2a/stream",
            json={
                "jsonrpc": "2.0",
                "id": trace_id,
                "method": "tasks/sendStream",
                "params": {
                    "task": task,
                    "context": context,
                    "_meta": meta,
                },
            },
        ) as resp:
            resp.raise_for_status()
            buffer = ""
            async for chunk in resp.aiter_bytes():
                # ``errors="replace"`` so a multi-byte char split across two
                # network chunks degrades to U+FFFD on one event instead of
                # exploding the whole stream. Rare on loopback, but cheap.
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buffer:
                    line, buffer = buffer.split("\n\n", 1)
                    line = line.strip()
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip():
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError as exc:
                                # A malformed SSE chunk used to be swallowed
                                # silently here — orchestrator spinner would
                                # spin forever with no signal that the peer
                                # had emitted garbage. Log + surface a
                                # warning event so the TUI can react and the
                                # user can see something is off.
                                preview = data[:200].replace("\n", " ")
                                log.warning(
                                    "A2A peer %s emitted malformed SSE data: %s (%s)",
                                    peer_id, preview, exc,
                                )
                                yield {
                                    "type": "warning",
                                    "message": (
                                        f"peer {peer_id} sent malformed event "
                                        f"(dropped): {preview}"
                                    ),
                                }


async def call_peer(
    *, peer_id: str, skill_id: str, input: dict, meta: dict,
) -> dict:
    """Non-streaming A2A RPC call (backward-compatible with skill-agent pattern)."""
    peers = _load_peers()
    url = peers.get(peer_id)
    if not url:
        raise RuntimeError(f"unknown peer: {peer_id}")

    # trust_env=False: see comment in delegate_task — A2A is loopback-only and
    # must never be routed through a user-configured proxy.
    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        resp = await client.post(
            f"{url}/a2a",
            json={
                "jsonrpc": "2.0",
                "id": meta.get("trace_id", "call"),
                "method": "tasks/send",
                "params": {
                    "skill_id": skill_id,
                    "input": input,
                    "_meta": meta,
                },
            },
        )
        resp.raise_for_status()
        return resp.json()
