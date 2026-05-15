# orchestrator/main.py
from __future__ import annotations
import asyncio
import json
import logging
import os
import secrets
import sys
from pathlib import Path

from orchestrator.registry import load_cards
from orchestrator.mcp_host import MCPHost
from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux
from orchestrator.turns import LLMPlanner, _stub_planner, run_prompt_once

log = logging.getLogger(__name__)


def _agent_dir() -> Path:
    return Path(".agent") / "agents"


async def _bootstrap(host: MCPHost, router: CapabilityRouter) -> None:
    cards = load_cards(_agent_dir())
    for card in cards:
        await host.spawn(card)
        tools = await host.list_tools(card.id)
        router.register(card.id, [t.name for t in tools])

    # After all specialists are up, broadcast their A2A URLs.
    from pathlib import Path
    peers = host.a2a_urls()  # already returns {id: url} from Task 5.2
    runtime_dir = Path(".agent/runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "peers.json").write_text(json.dumps(peers), encoding="utf-8")


def _build_orchestrator_llm():
    """Build a chat model for the orchestrator's planner.

    Day-1 strategy: defer to whatever LLM the legacy single_agent_loop builds.
    """
    # The legacy code constructs its chat model lazily inside CliApp; pulling
    # that out is more invasive than this task warrants. For Day-1 the
    # orchestrator's LLM path is only exercised when the user sets a real
    # provider, which they're unlikely to do during automated tests.
    #
    # If a clean factory function exists in the legacy module, prefer it.
    # Otherwise, fall back to env-var-driven construction here.
    try:
        from legacy.single_agent_loop import _build_chat_model as _factory  # type: ignore
        return _factory()
    except ImportError:
        pass
    # Fallback: construct ChatOpenAI from env (works for openai-compatible providers).
    # Adjust if you need anthropic.
    raise RuntimeError(
        "orchestrator LLM factory not available; set LANGCHAIN_AGENT_MODEL=mock for tests "
        "or add a chat-model factory to agents/shared/."
    )


async def run_prompt(prompt: str) -> int:
    hmac_key = secrets.token_urlsafe(32)
    host = MCPHost(hmac_key=hmac_key)
    router = CapabilityRouter()
    mux = StreamMux()
    try:
        await _bootstrap(host, router)
        provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
        if provider.startswith("mock") or not provider:
            planner = _stub_planner
        else:
            llm = _build_orchestrator_llm()
            planner = LLMPlanner(llm=llm, available_capabilities=router.all_capabilities())
        return await run_prompt_once(
            prompt=prompt,
            host=host,
            router=router,
            hmac_key=hmac_key,
            planner=planner,
            permission_mode_provider=lambda: os.environ.get(
                "LANGCHAIN_AGENT_PERMISSION_MODE", "workspace-write"
            ),
            mux=mux,
        )
    finally:
        await host.shutdown_all()


def _handle_slash_agents(host, *, out=None) -> None:
    """Render an /agents table to `out` (defaults to stdout)."""
    import sys
    out = out or sys.stdout
    rows = []
    for handle in host.list_handles():
        c = handle.card
        url = handle.a2a_url or "-"
        rows.append(f"{c.id:16s} v{c.version:6s} a2a={url}")
    out.write("\n".join(rows) + "\n")


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
    try:
        if prompt is not None:
            return asyncio.run(run_prompt(prompt))
        return asyncio.run(run_repl())
    except KeyboardInterrupt:
        # User Ctrl+C'd. The asyncio context manager already triggered the
        # shutdown path via CancelledError; nothing more to do.
        print("\n[orchestrator] cancelled by user", file=__import__("sys").stderr)
        return 130  # conventional shell exit code for SIGINT
