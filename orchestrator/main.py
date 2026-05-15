# orchestrator/main.py
from __future__ import annotations
import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path

from orchestrator.registry import load_cards
from orchestrator.mcp_host import MCPHost
from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux
from orchestrator.graph import build_graph

log = logging.getLogger(__name__)


def _agent_dir() -> Path:
    return Path(".agent") / "agents"


async def _bootstrap(host: MCPHost, router: CapabilityRouter) -> None:
    cards = load_cards(_agent_dir())
    for card in cards:
        await host.spawn(card)
        tools = await host.list_tools(card.id)
        router.register(card.id, [t.name for t in tools])


def _stub_planner(state):
    """Phase-5 deterministic planner used until Phase 6 plugs in an LLM.

    Accepts user_input in the form 'CAPABILITY:ARG' and routes to that
    capability with arguments {"path": ARG} (suitable for read_file)."""
    text = state["user_input"]
    if ":" in text:
        cap, _, arg = text.partition(":")
        return {"capability": cap.strip(), "arguments": {"path": arg.strip()}}
    raise ValueError(
        "Phase-5 stub planner: expected 'CAPABILITY:ARG' input "
        "(LLM planner ships in Phase 6)"
    )


async def run_prompt(prompt: str) -> int:
    hmac_key = secrets.token_urlsafe(32)
    host = MCPHost(hmac_key=hmac_key)
    router = CapabilityRouter()
    mux = StreamMux()
    try:
        await _bootstrap(host, router)
        mode = os.environ.get("LANGCHAIN_AGENT_PERMISSION_MODE", "workspace-write")
        graph = build_graph(
            router=router, host=host, planner=_stub_planner,
            hmac_key=hmac_key, mode=mode,
        )
        result = await graph.ainvoke({"user_input": prompt, "trace_id": "t1"})
        if result.get("error"):
            mux.emit(
                agent_id="orchestrator", trace_id="t1",
                chunk=f"error: {result['error']}\n",
            )
            return 1

        cap = result.get("capability", "")
        owner = router.resolve(cap) if cap else "orchestrator"
        call_result = result.get("result")
        # MCP returns a CallToolResult-like object with `.content` (a list of
        # content blocks) and `.isError`. We render the text blocks.
        contents = getattr(call_result, "content", None)
        if contents is None and isinstance(call_result, dict):
            contents = call_result.get("content")
        for piece in (contents or []):
            text = getattr(piece, "text", None)
            if text is None and isinstance(piece, dict):
                text = piece.get("text", "")
            if text:
                mux.emit(agent_id=owner, trace_id="t1", chunk=text + "\n")
        return 0
    finally:
        await host.shutdown_all()


async def run_repl() -> int:
    mux = StreamMux()
    mux.emit(
        agent_id="orchestrator", trace_id="boot",
        chunk=(
            "multi-agent REPL not fully implemented in Phase 5 — "
            "try `python cli.py --single` for now.\n"
        ),
    )
    return 0


def main(*, prompt: str | None = None) -> int:
    if prompt is not None:
        return asyncio.run(run_prompt(prompt))
    return asyncio.run(run_repl())
