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

    # After all specialists are up, broadcast their A2A URLs.
    from pathlib import Path
    peers = host.a2a_urls()  # already returns {id: url} from Task 5.2
    runtime_dir = Path(".agent/runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "peers.json").write_text(json.dumps(peers), encoding="utf-8")


class LLMPlanner:
    """Plans the next capability + arguments by asking the LLM."""

    _SYSTEM = (
        "You are the orchestrator's planning brain. The available capabilities are listed below. "
        "Reply with ONLY a JSON object of the form "
        '{"capability": "<name>", "arguments": {<args>}}. '
        "No prose, no markdown fence."
    )

    def __init__(self, *, llm, available_capabilities: list[str]):
        self._llm = llm
        self._caps = available_capabilities

    def __call__(self, state) -> dict:
        prompt = (
            f"Available capabilities: {self._caps}\n\n"
            f"User: {state['user_input']}"
        )
        out = self._llm.invoke([
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": prompt},
        ])
        text = out.content.strip()
        # Strip accidental code fences
        if text.startswith("```"):
            # Strip opening fence (with optional language tag) and closing fence
            lines = text.split("\n")
            # Drop first line (```json or ```)
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            # Drop trailing fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return json.loads(text)


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

        # Planner selection:
        #   - Mock provider OR no provider configured → deterministic stub planner.
        #   - Real provider → LLMPlanner.
        provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
        if provider.startswith("mock") or not provider:
            planner = _stub_planner
        else:
            llm = _build_orchestrator_llm()
            planner = LLMPlanner(llm=llm, available_capabilities=router.all_capabilities())

        graph = build_graph(
            router=router, host=host, planner=planner,
            hmac_key=hmac_key, mode=mode,
        )

        # Start telemetry tail so A2A peer invocations surface in the unified stream.
        from orchestrator import telemetry
        telemetry.reset_log()
        stop = asyncio.Event()
        tail_task = asyncio.create_task(telemetry.tail(mux, stop))
        try:
            result = await graph.ainvoke({"user_input": prompt, "trace_id": "t1"})
            # Give the tail a moment to flush any in-flight events.
            await asyncio.sleep(0.1)
        finally:
            stop.set()
            try:
                await asyncio.wait_for(tail_task, timeout=2.0)
            except asyncio.TimeoutError:
                tail_task.cancel()

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
