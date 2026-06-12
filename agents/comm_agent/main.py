"""comm-agent process entrypoint.

Launched by orchestrator via:
    python -m agents.comm_agent.main

Exposes:
  - MCP stdio: comm.* tools (see mcp_tools.py)
  - Public A2A HTTP (via Caddy): /.well-known/agent.json + /a2a + /a2a/stream

Optional environment:
  COMM_AGENT_MY_PEER_ID     — our self-identity for outbound grants
                              (default: "ww-agent")
  COMM_AGENT_PUBLIC_HOST    — host name in Caddyfile (default: None → :8443 + tls internal)
  COMM_AGENT_PUBLIC_PORT    — Caddy listen port (default: 8443)
  COMM_AGENT_SELF_HMAC      — env var name holding our inbound HMAC secret
                              (default: "COMM_AGENT_SELF_HMAC")
  CADDY_BINARY              — caddy executable (default: "caddy")
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn

from agent_paths import config_dir
from agents.comm_agent.a2a_protocol import build_app
from agents.comm_agent.agent_card import build_self_card
from agents.comm_agent.caddy_supervisor import CaddySupervisor, render_caddyfile
from agents.comm_agent.mcp_tools import build_comm_tool_specs
from agents.comm_agent.peer_registry import PeerRegistry
from agents.shared.mcp_server import build_server

log = logging.getLogger(__name__)


async def _noop_stream(skill: str, params: dict, claims: dict) -> AsyncIterator[dict]:
    """Inbound stream stub (MVP). Spec §3.3 lists task.delegate as a skill we
    expose, but a real LLM-backed implementation is out of scope for this
    initial cut — we send back a polite refusal until v1.1."""
    yield {"type": "task", "state": "working", "message": "inbound delegation not yet implemented"}
    yield {"type": "task", "state": "failed", "error": "task.delegate inbound MVP returns 'not implemented'"}


async def _noop_dispatch(skill: str, params: dict, claims: dict) -> dict:
    if skill == "status/query":
        return {"state": "idle", "current_task": None, "last_error": None}
    return {"error": f"skill {skill!r} not implemented inbound (MVP)"}


def _pick_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def amain() -> int:
    my_peer_id = os.environ.get("COMM_AGENT_MY_PEER_ID", "ww-agent")
    public_host = os.environ.get("COMM_AGENT_PUBLIC_HOST") or None
    public_port = int(os.environ.get("COMM_AGENT_PUBLIC_PORT", "8443"))
    self_secret_env = os.environ.get("COMM_AGENT_SELF_HMAC", "COMM_AGENT_SELF_HMAC")
    self_secret = os.environ.get(self_secret_env, "")
    if not self_secret:
        log.warning(
            "no inbound HMAC secret in env %s — inbound A2A calls will all 401",
            self_secret_env,
        )
        self_secret = "DISABLED-INBOUND-" + os.urandom(8).hex()

    # 1. Build FastAPI app (inbound A2A) on an ephemeral port behind Caddy.
    upstream_port = _pick_free_port()
    public_url = (
        f"https://{public_host}:{public_port}" if public_host
        else f"https://127.0.0.1:{public_port}"
    )
    self_card = build_self_card(
        name=f"comm-{my_peer_id}",
        description="W&W Agent comm-agent (A2A v0.3)",
        public_url=public_url,
        version="1.0.0",
    )
    # Cross-process replay defense: under uvicorn --workers N each worker
    # is its own process, so an in-memory NonceCache wouldn't see other
    # workers' nonces. The SQLite store gives all workers one shared view.
    from agents.shared.authz import SqliteNonceStore
    from agent_paths import runtime_dir as _runtime_dir
    nonce_db = _runtime_dir() / "comm-nonces.db"
    nonce_db.parent.mkdir(parents=True, exist_ok=True)
    nonce_store = SqliteNonceStore(nonce_db)

    app = build_app(
        self_card=self_card,
        hmac_secret=self_secret,
        my_peer_id=my_peer_id,
        skill_dispatcher=_noop_dispatch,
        stream_dispatcher=_noop_stream,
        nonce_cache=nonce_store,
    )

    # 2. Start uvicorn on the upstream port.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=upstream_port,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)
    uvicorn_task = asyncio.create_task(server.serve())
    # Wait for server to be ready.
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)

    # 3. Render Caddyfile + start Caddy supervisor.
    caddy_dir = config_dir() / "caddy"
    caddy_dir.mkdir(parents=True, exist_ok=True)
    caddyfile = caddy_dir / "comm-agent.caddy"
    sup = CaddySupervisor(
        caddyfile_path=caddyfile,
        binary=os.environ.get("CADDY_BINARY", "caddy"),
    )
    sup.set_caddyfile(render_caddyfile(
        public_host=public_host,
        listen_port=public_port,
        upstream_port=upstream_port,
        access_log=caddy_dir / "access.log",
    ))
    try:
        await sup.start()
    except Exception as exc:  # noqa: BLE001 - Caddy is optional; degrade gracefully
        log.warning("could not start caddy (%s); comm-agent will run with stdio MCP only", exc)

    # 4. Write the public URL to runtime dir so orchestrator can discover us.
    agent_id = os.environ.get("AGENT_ID", "comm-agent")
    from agent_paths import runtime_dir
    rt_dir = runtime_dir()
    rt_dir.mkdir(parents=True, exist_ok=True)
    (rt_dir / f"{agent_id}.a2a-url").write_text(public_url, encoding="utf-8")

    # 5. Build the comm.* MCP tool list backed by the on-disk peer registry.
    reg = PeerRegistry(config_dir() / "comm_peers.json")
    tools = build_comm_tool_specs(reg=reg, my_peer_id=my_peer_id)
    _proxy, runner = build_server(name="comm-agent", tools=tools)

    try:
        await runner()
    finally:
        await sup.stop()
        server.should_exit = True
        try:
            await asyncio.wait_for(uvicorn_task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
