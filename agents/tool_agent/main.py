"""tool-agent process entrypoint.

Launched by orchestrator via:
    python -m agents.tool_agent.main

Exposes MCP stdio tools + A2A HTTP (RPC + SSE streaming) for agent-level tasks.
"""
from __future__ import annotations
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator

from agents.shared.mcp_server import build_server, ToolSpec
from agents.shared.a2a_server import A2AServer, A2AHandler, A2AStreamHandler
from agents.tool_agent.tool_executor import build_tool_specs, execute_tool, make_langchain_tools
# Import the ReAct loop (and its langchain/langgraph dependencies) at module
# load time, not lazily inside the SSE handler. Lazy import worked, but Python
# imports are CPU-bound and synchronous; importing langchain+langgraph for the
# first time costs 6-8 seconds, and doing it AFTER the first SSE yield blocks
# the asyncio event loop the whole time, preventing uvicorn from actually
# flushing the buffered chunks to the socket. The user sees the orchestrator
# spinner stuck on `Delegating to tool-agent...` for the entire import window.
# Paying the cost once at subprocess startup eliminates that perceived hang.
from agents.tool_agent.agent_loop import ToolAgentLoop

log = logging.getLogger(__name__)


def _build_mock_chat_model():
    """A FakeListChatModel that survives create_react_agent's piping.

    We deliberately do NOT reuse ``agents.shared.mock_chat_model.MockChatModel``
    here: that class is a minimal stand-in used in unit tests that drive the
    LLM directly, but it is not a langchain ``Runnable`` and so cannot be
    composed via ``prompt | model`` inside ``create_react_agent``. The langchain
    built-in ``FakeListChatModel`` IS a real BaseChatModel, so the ReAct loop
    runs end-to-end with it.

    The subclass overrides ``bind_tools`` because FakeListChatModel's default
    raises ``NotImplementedError`` — the mock never emits tool calls anyway,
    so accepting any tool list and returning self is the right test behavior.
    """
    import os
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    class _ToolBindingFakeListChatModel(FakeListChatModel):
        def bind_tools(self, tools, **_kw):
            return self

    raw = os.environ.get("MOCK_TOOL_AGENT_SCRIPT", "ok")
    responses = raw.split("||")
    return _ToolBindingFakeListChatModel(responses=responses)


def _build_agent_llm_sync():
    """Synchronous LLM construction. Wrapped by ``get_llm`` for async use."""
    import os
    raw = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
    if raw.startswith("mock"):
        return _build_mock_chat_model()

    try:
        from config import build_llm, hydrate_env_from_credentials, load_active_config
        hydrate_env_from_credentials()
        return build_llm(load_active_config())
    except Exception:
        log.exception("Failed to build tool-agent LLM, falling back to mock")
        return _build_mock_chat_model()


# Cached LLM future. The first ``await get_llm()`` kicks off construction in a
# worker thread (ChatOpenAI.__init__ alone takes ~6s on cold tiktoken/httpx
# init); subsequent awaiters get the cached instance instantly. Crucially,
# offloading to a thread keeps the asyncio event loop responsive so uvicorn
# can flush the eager `{"type": "thinking"}` SSE chunk to the orchestrator
# while the LLM is still being constructed in the background.
_llm_future: asyncio.Future | None = None


async def get_llm():
    global _llm_future
    if _llm_future is None:
        _llm_future = asyncio.ensure_future(asyncio.to_thread(_build_agent_llm_sync))
    return await _llm_future


def prewarm_llm() -> None:
    """Kick off LLM construction in the background at process startup so the
    first task delegation does not pay the cold-start cost."""
    global _llm_future
    if _llm_future is None:
        _llm_future = asyncio.ensure_future(asyncio.to_thread(_build_agent_llm_sync))


async def handle_tool_task_stream(payload: dict) -> AsyncIterator[dict[str, Any]]:
    """Top-level streaming dispatch for the ``tool.task`` A2A endpoint.

    Extracted out of ``amain``'s inner closure so tests can ``await`` the
    real production code path directly (the previous test had to re-implement
    the logic, which meant a regression in the closure wouldn't be caught).

    Sequence:
      1. Extract ``task`` / ``context`` / ``meta`` from the payload shape
         that ``A2AServer.stream_endpoint`` produces.
      2. Verify the orchestrator-minted authz_grant (refuses unauthenticated
         calls when ``AUTHZ_HMAC_KEY`` is set — i.e. in production).
      3. Validate the grant's ``permission_mode`` claim against the known
         whitelist; reject unknown modes (fail-loud rather than fail-quiet).
      4. Forward to ``_run_agent_streaming`` with the validated mode.
    """
    task = payload.get("task", "")
    if not task:
        yield {"type": "error", "message": "missing 'task' in payload"}
        return
    context = payload.get("context", "") or ""

    meta = payload.get("meta") or {}
    grant = meta.get("authz_grant")
    key = os.environ.get("AUTHZ_HMAC_KEY")
    if grant is not None and key:
        from agents.shared.authz import verify_grant
        try:
            claims = verify_grant(grant, key=key, requested_tool="tool.task")
        except Exception as exc:
            yield {"type": "error", "message": f"authz: {exc}"}
            return
        permission_mode = str(claims.get("permission_mode") or "danger-full-access")
        # The PermissionGate that minted this grant rejects unknown modes at
        # sign() time — but a forged/tampered grant could still carry one,
        # and an unknown mode would silently degrade to "no tools bound"
        # which the user would experience as a mute / confused agent. Fail
        # loud instead so the operator sees the cause.
        from agents.shared.permission_modes import _MODE_WHITELIST
        if permission_mode not in _MODE_WHITELIST:
            yield {
                "type": "error",
                "message": (
                    f"authz: unknown permission_mode {permission_mode!r} in grant; "
                    f"expected one of {sorted(_MODE_WHITELIST)}."
                ),
            }
            return
    elif key:
        yield {
            "type": "error",
            "message": (
                "tool-agent refused tool.task: no authz_grant in payload meta. "
                "Orchestrator should mint one via PermissionGate.sign()."
            ),
        }
        return
    else:
        permission_mode = "danger-full-access"

    async for event in _run_agent_streaming(
        task, context=context, permission_mode=permission_mode,
    ):
        yield event


async def _run_agent_streaming(
    task: str, *, context: str = "", permission_mode: str = "danger-full-access",
) -> AsyncIterator[dict[str, Any]]:
    """Stream agent events for SSE consumption.

    Yields ``{"type": "thinking"}`` IMMEDIATELY, before any of the heavy
    cold-start work (langchain/langgraph import, LLM construction, tool binding,
    first LLM token). On a fresh subprocess this work takes 7-10s; without an
    eager first event the orchestrator's `Delegating to tool-agent...` spinner
    sits silently the whole time and the user concludes it has hung.

    Internally uses a queue + driver-task pattern so the ``clarify`` tool's
    wrapper can inject ``clarify_request`` events into the SSE stream from
    inside the ReAct loop (see ``clarify_bridge``). The driver pulls agent
    events into the queue; this generator pulls from the queue; the wrapper
    can ``put`` out-of-band events on the same queue while it awaits the
    user's answer.
    """
    yield {"type": "thinking"}

    try:
        llm = await get_llm()
        tools = make_langchain_tools(mode=permission_mode)
        agent = ToolAgentLoop(llm=llm, tools=tools, context=context)
    except Exception as exc:
        log.exception("tool-agent setup failed before agent.run")
        yield {"type": "error", "message": f"tool-agent setup failed: {exc}"}
        return

    from agents.tool_agent import clarify_bridge

    queue: asyncio.Queue = asyncio.Queue()
    clarify_bridge.set_event_queue(queue)

    # ToolAgentLoop.run yields its own initial `thinking` event; that's fine —
    # the orchestrator's status panel is idempotent across repeated thinkings.
    sentinel: dict[str, Any] = {"__sentinel__": True}

    async def _drive() -> None:
        try:
            async for event in agent.run(task=task):
                await queue.put(event)
        except Exception as exc:
            log.exception("ReAct loop crashed in driver task")
            await queue.put({"type": "error", "message": str(exc)})
        finally:
            await queue.put(sentinel)

    driver = asyncio.create_task(_drive())
    try:
        while True:
            event = await queue.get()
            if event is sentinel:
                break
            yield event
    finally:
        if not driver.done():
            driver.cancel()
            try:
                await driver
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                log.exception("driver task raised during cleanup")


async def amain() -> None:
    # 1. Start A2A server first so we know our bound URL.

    # Non-streaming dispatch (backward compatible + "tool.task" agent-level).
    async def a2a_dispatch(skill_id: str, input: dict, meta: dict) -> dict:
        if skill_id == "_clarify_response":
            # Out-of-band response from the orchestrator: the user
            # answered a ``clarify_request`` we emitted earlier. Resolve
            # the matching pending future so the wrapper unblocks.
            from agents.tool_agent import clarify_bridge

            request_id = str(input.get("request_id") or "")
            answer = str(input.get("answer") or "")
            if not request_id:
                return {"error": "missing 'request_id' in clarify response"}
            ok = clarify_bridge.resolve(request_id, answer)
            return {"resolved": ok}

        if skill_id == "tool.task":
            # ``tool.task`` MUST go through the streaming dispatch
            # (``handle_tool_task_stream``), which verifies the authz_grant
            # and mode-gates the tool set. The non-streaming RPC variant
            # used to default to ``danger-full-access`` and had no
            # production caller — removed to close that latent escape
            # hatch. Anyone hitting this branch should switch to the
            # streaming endpoint instead.
            return {
                "error": (
                    "tool.task is only available via the streaming endpoint "
                    "(/a2a/stream). The non-streaming RPC path does not "
                    "enforce permission-mode gating and has been removed."
                ),
            }

        if not skill_id.startswith("tool."):
            return {"error": f"tool-agent does not expose {skill_id}"}
        tool_name = skill_id[len("tool."):]

        from agents.shared.telemetry import emit_event
        emit_event(
            agent_id="tool-agent",
            trace_id=meta.get("trace_id", "?"),
            message=f"(via A2A from {meta.get('agent_caller', '?')}) {skill_id}",
        )

        args = {**input, "_meta": meta}
        result = await execute_tool(tool_name, args)
        return {"result": result}

    # Streaming dispatch delegates to the module-level ``handle_tool_task_stream``
    # so the same code path is reachable from unit tests (without spinning up
    # the full ``amain`` entrypoint or re-implementing the auth logic).
    a2a = A2AServer(
        handler=A2AHandler(handler=a2a_dispatch),
        stream_handler=A2AStreamHandler(handler=handle_tool_task_stream),
    )
    await a2a.start()

    # 2. Write the A2A URL to the runtime dir so orchestrator can discover it.
    agent_id = os.environ.get("AGENT_ID", "tool-agent")
    runtime_dir = Path(".agent/runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"{agent_id}.a2a-url").write_text(a2a.base_url, encoding="utf-8")

    # Pre-build the LLM in a worker thread now, while the MCP server is still
    # spinning up. By the time the first orchestrator delegation arrives, the
    # cached future is usually already resolved.
    prewarm_llm()

    # 3. Build the MCP server (existing logic, backward compatible).
    specs = build_tool_specs()

    def _make_handler(tool_name: str):
        async def _h(args: dict) -> Any:
            return await execute_tool(tool_name, args)
        return _h

    guarded = [
        ToolSpec(s.name, s.description, s.input_schema, _make_handler(s.name))
        for s in specs
    ]
    _proxy, runner = build_server(name="tool-agent", tools=guarded)

    try:
        await runner()
    finally:
        await a2a.stop()


def main() -> int:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
