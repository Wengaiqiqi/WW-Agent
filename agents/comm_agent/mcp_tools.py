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
from urllib.parse import urlparse

from pathlib import Path

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


# ---------------------------------------------------------------------------
# Secret persistence — HMAC secrets survive process restarts.
# ---------------------------------------------------------------------------

def _secrets_path() -> Path:
    """Return the path to the on-disk secrets file."""
    from agent_paths import config_dir
    return config_dir() / "comm_secrets.env"


def load_persisted_secrets() -> None:
    """Load persisted HMAC secrets into ``os.environ``.

    Called once at comm-agent startup (before building tools) so that
    ``resolve_secret`` can find secrets that were saved by a previous run.
    """
    path = _secrets_path()
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _persist_secret(env_name: str, value: str) -> None:
    """Append or update a secret in the on-disk secrets file."""
    path = _secrets_path()
    lines: list[str] = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{env_name}="):
            lines[i] = f"{env_name}={value}"
            found = True
            break
    if not found:
        lines.append(f"{env_name}={value}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist secret to %s: %s", path, exc)


def _remove_secret(env_name: str) -> None:
    """Remove a secret from the on-disk secrets file."""
    path = _secrets_path()
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    filtered = [l for l in lines if not l.strip().startswith(f"{env_name}=")]
    try:
        path.write_text("\n".join(filtered) + ("\n" if filtered else ""), encoding="utf-8")
    except OSError as exc:
        log.warning("could not update secrets file %s: %s", path, exc)


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
        scheme = (urlparse(url).scheme or "").lower()
        if scheme not in ("http", "https"):
            return _err(f"url must start with http:// or https://; got {url!r}")
        warnings: list[str] = []
        if scheme == "http":
            # The HMAC grant authenticates the request but is NOT encrypted in
            # transit; over plain http:// it can be sniffed and (within its 60s
            # TTL) replayed against this peer. Warn loudly but allow it for
            # trusted localhost / VPN links.
            warnings.append(
                "url uses http:// — the HMAC grant is sent in cleartext; prefer "
                "https:// (Caddy terminates TLS) unless this is a trusted "
                "localhost/VPN link"
            )
        env_name = _env_var_name_for(peer_id)
        # _env_var_name_for collapses peer_ids that differ only in punctuation
        # ("peer-1" and "peer.1" both map to COMM_PEER_PEER_1_HMAC). If a
        # different peer_id already claimed this env var, refuse — otherwise
        # the second add overwrites the first peer's secret silently and the
        # original peer's outbound grants would be signed with the wrong key.
        for existing in reg.list_peers():
            if (
                existing.peer_id != peer_id
                and existing.hmac_secret_ref == env_name
            ):
                return _err(
                    f"peer_id {peer_id!r} collides with already-registered "
                    f"{existing.peer_id!r} on env var {env_name!r}; "
                    "choose a distinct peer_id (different alphanumerics, not "
                    "just punctuation)"
                )
        os.environ[env_name] = secret_value
        _persist_secret(env_name, secret_value)
        peer = Peer(
            peer_id=peer_id,
            display_name=display_name,
            url=url,
            hmac_secret_ref=env_name,
            tls_verify=args.get("tls_verify", True),
            tls_pinned_sha256=args.get("tls_pinned_sha256"),
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
        note = f"secret persisted to {_secrets_path()}"
        if warnings:
            note = " | ".join(warnings) + " | " + note
        return _ok({
            "peer_id": peer_id,
            "env_var_name": env_name,
            "fetched_card": fetched_card,
            "warnings": warnings,
            "note": note,
        })

    # ---- comm.remove_peer ----
    async def remove_peer(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        if not peer_id:
            return _err("peer_id is required")
        peer = reg.get(peer_id)
        if peer is not None:
            _remove_secret(peer.hmac_secret_ref)
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
                    "tls_verify": {
                        "type": "boolean",
                        "description": (
                            "Whether to verify the peer's TLS certificate "
                            "(default true). Set to false for self-signed certs "
                            "on trusted LAN links."
                        ),
                    },
                    "tls_pinned_sha256": {
                        "type": "string",
                        "description": (
                            "Optional SHA-256 fingerprint of the peer's "
                            "certificate, lowercase hex. When set, the client "
                            "accepts any self-signed cert (pinning enforcement "
                            "deferred to v1.1; HMAC signing defeats MITM in "
                            "the meantime)."
                        ),
                    },
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

    # ---- comm.delegate ----
    async def delegate(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        task = args.get("task", "")
        stream = args.get("stream", True)
        if not peer_id or not task:
            return _err("peer_id and task are required")
        peer = reg.get(peer_id)
        if peer is None:
            return _err(f"unknown peer {peer_id!r}; run comm.add_peer first")
        try:
            secret = reg.resolve_secret(peer)
        except PeerRegistryError as exc:
            return _err(str(exc))
        client = _make_client_for(peer, secret, my_peer_id, transport=_transport())
        try:
            # Always exercise the SSE stream (cheaper than maintaining two code
            # paths); we collect events when stream=False and return a summary.
            loop = asyncio.get_running_loop()
            start = loop.time()
            events: list[dict] = []
            final: Any = None
            saw_completion = False
            stream_error: str | None = None
            async for event in client.stream(method="message/stream", params={
                "message": {"role": "user", "parts": [{"text": task}]},
                "context_id": args.get("context"),
            }, skill="task.delegate"):
                events.append(event)
                if event.get("type") == "task" and event.get("state") == "completed":
                    final = event.get("result")
                    saw_completion = True
                elif event.get("type") == "error":
                    stream_error = str(event.get("message") or event)
        except Exception as exc:
            log.exception("comm.delegate: unexpected error calling %s", peer_id)
            return _err(f"comm.delegate failed: {exc}")
        duration_ms = int((loop.time() - start) * 1000)
        # If the peer never sent a `task: completed` event and didn't surface
        # an explicit error, the stream was truncated. Treating that as
        # `ok=true, final_result=null` makes silent failures look like an
        # empty answer to the planner — flag it.
        if not saw_completion:
            return _err(
                stream_error
                or f"peer {peer_id!r} ended stream after "
                f"{len(events)} events without a completion event"
            )
        if stream:
            # Return ALL events as one JSON blob — orchestrator's stream_mux
            # consumes this and re-renders. (See Task 11 for the live-stream
            # variant using MCP progress notifications; this MVP returns the
            # full transcript in one shot.)
            return json.dumps({
                "ok": True, "events": events,
                "final_result": final, "duration_ms": duration_ms,
            }, ensure_ascii=False)
        return json.dumps({
            "ok": True,
            "events_count": len(events),
            "final_result": final,
            "duration_ms": duration_ms,
        }, ensure_ascii=False)

    # ---- comm.chat ----
    async def chat(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        message = args.get("message", "")
        context_id = args.get("context_id")
        if not peer_id or not message:
            return _err("peer_id and message are required")
        peer = reg.get(peer_id)
        if peer is None:
            return _err(f"unknown peer {peer_id!r}; run comm.add_peer first")
        try:
            secret = reg.resolve_secret(peer)
        except PeerRegistryError as exc:
            return _err(str(exc))
        client = _make_client_for(peer, secret, my_peer_id, transport=_transport())
        try:
            result = await client.call(method="message/send", params={
                "message": {"role": "user", "parts": [{"text": message}]},
                "context_id": context_id,
            }, skill="chat.message")
        except A2AClientError as exc:
            return _err(str(exc))
        except Exception as exc:
            log.exception("comm.chat: unexpected error calling %s", peer_id)
            return _err(f"comm.chat failed: {exc}")
        return json.dumps({
            "ok": True,
            "reply": result.get("reply", ""),
            "context_id": result.get("context_id"),
        }, ensure_ascii=False)

    # ---- comm.status ----
    async def status(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        peer = reg.get(peer_id)
        if peer is None:
            return _err(f"unknown peer {peer_id!r}; run comm.add_peer first")
        try:
            secret = reg.resolve_secret(peer)
        except PeerRegistryError as exc:
            return _err(str(exc))
        client = _make_client_for(peer, secret, my_peer_id, transport=_transport())
        try:
            result = await client.call(method="status/query", params={}, skill="status.query")
        except A2AClientError as exc:
            return _err(str(exc))
        except Exception as exc:
            log.exception("comm.status: unexpected error calling %s", peer_id)
            return _err(f"comm.status failed: {exc}")
        return json.dumps({"ok": True, "status": result}, ensure_ascii=False)

    specs.extend([
        ToolSpec(
            name="comm.delegate",
            description=(
                "Delegate a free-form task to a remote A2A agent. When stream=true "
                "(default) returns all SSE events in one blob; when stream=false "
                "returns only the final result + counts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "peer_id": {"type": "string"},
                    "task": {"type": "string"},
                    "context": {"type": "string"},
                    "stream": {"type": "boolean"},
                },
                "required": ["peer_id", "task"],
            },
            handler=delegate,
        ),
        ToolSpec(
            name="comm.chat",
            description=(
                "Append one turn to a chat session with a remote A2A agent. Pass "
                "context_id=null first time; server returns one to keep for "
                "subsequent turns."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "peer_id": {"type": "string"},
                    "message": {"type": "string"},
                    "context_id": {"type": ["string", "null"]},
                },
                "required": ["peer_id", "message"],
            },
            handler=chat,
        ),
        ToolSpec(
            name="comm.status",
            description="Query the current state of a remote A2A agent.",
            input_schema={
                "type": "object",
                "properties": {"peer_id": {"type": "string"}},
                "required": ["peer_id"],
            },
            handler=status,
        ),
    ])
    return specs
