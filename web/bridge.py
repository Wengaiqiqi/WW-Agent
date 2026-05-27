"""Streaming bridge from the web surface to the orchestrator core.

Mirrors gateway.runner's bootstrap+dispatch but YIELDS events instead of
returning final text only. Reuses runner's helpers and shares its concurrency
guard so a web turn and an in-process gateway turn never run concurrently
(they share .agent/runtime files)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional

from gateway.runner import (
    _CONCURRENCY_GUARD,
    _build_planner,
    _build_planner_context,
)
from web import config

log = logging.getLogger(__name__)


def _user_workspace(user_id: str) -> Path:
    from agent_paths import config_dir

    safe = user_id or "anon"
    ws = config_dir() / "web" / "workspaces" / safe
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _set_or_clear(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


@contextlib.contextmanager
def _web_turn_env(*, user_id: str, model_id: str) -> Iterator[Path]:
    """Set the per-turn env (memory user, forced workspace-write, per-user
    workspace root, selected model) and restore the prior values on exit."""
    prev = {
        k: os.environ.get(k)
        for k in (
            "LANGCHAIN_AGENT_MEMORY_USER",
            "LANGCHAIN_AGENT_PERMISSION_MODE",
            "LANGCHAIN_AGENT_WORKSPACE_ROOT",
            "LANGCHAIN_AGENT_MODEL",
        )
    }
    ws = _user_workspace(user_id)
    try:
        _set_or_clear("LANGCHAIN_AGENT_MEMORY_USER", user_id or None)
        _set_or_clear("LANGCHAIN_AGENT_PERMISSION_MODE", config.WEB_PERMISSION_MODE)
        _set_or_clear("LANGCHAIN_AGENT_WORKSPACE_ROOT", str(ws))
        _set_or_clear("LANGCHAIN_AGENT_MODEL", model_id or None)
        yield ws
    finally:
        for k, v in prev.items():
            _set_or_clear(k, v)


async def dispatch_decision_stream(
    *,
    decision: dict,
    prompt: str,
    host: Any,
    router: Any,
    hmac_key: str,
    trace_id: str,
    history_context: str,
    delegate: Optional[Any] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE events for the planner's decision (mirrors runner's three
    branches). On any error, yields an ``error`` event then a ``done`` so the
    browser stream always terminates cleanly."""
    capability = (decision.get("capability") or "").strip()

    # Branch A: prose answer, no dispatch.
    if not capability:
        text = (decision.get("response") or "").strip()
        yield {"type": "text", "chunk": text}
        yield {"type": "done", "text": text}
        return

    # Branch B: A2A delegation -- forward the specialist's event stream.
    if capability == "tool.task" or capability.startswith("skill."):
        from orchestrator.delegation import delegate_via_a2a_stream

        try:
            async for event in delegate_via_a2a_stream(
                capability=capability,
                arguments=decision.get("arguments") or {},
                user_input=prompt,
                hmac_key=hmac_key,
                trace_id=trace_id,
                permission_mode=config.WEB_PERMISSION_MODE,
                history_context=history_context,
                delegate=delegate,
            ):
                yield event
        except Exception as exc:  # noqa: BLE001
            log.exception("web: A2A delegate failed")
            yield {"type": "error", "message": f"{capability}: {exc}"}
            yield {"type": "done", "text": ""}
        return

    # Branch C: simple MCP capability via TurnRunner (no token streaming).
    from orchestrator.turns import TurnRunner

    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key=hmac_key,
        permission_mode_provider=lambda: config.WEB_PERMISSION_MODE,
        planner=lambda _state, _d=decision: _d,
    )
    try:
        result = await runner.run(prompt, trace_id=trace_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("web: MCP dispatch failed")
        yield {"type": "error", "message": f"{capability}: {exc}"}
        yield {"type": "done", "text": ""}
        return
    if result.error:
        yield {"type": "error", "message": result.error}
        yield {"type": "done", "text": ""}
        return
    text = (result.text or "").strip()
    yield {"type": "text", "chunk": text}
    yield {"type": "done", "text": text}


async def run_turn_streaming(
    prompt: str,
    *,
    trace_id: str = "web1",
    session_key: str = "",
    user_id: str = "",
    model_id: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """Run one orchestrator turn and yield SSE event dicts.

    Serialised on the shared concurrency guard. Sets the per-turn env scope,
    bootstraps a private MCPHost, runs the planner, streams the dispatch, then
    appends the final pair to session_store (planner context). The route layer
    is responsible for persisting to SQLite for the UI."""
    if not prompt or not prompt.strip():
        yield {"type": "done", "text": ""}
        return

    async with _CONCURRENCY_GUARD:
        async for ev in _stream_off_loop(
            prompt,
            trace_id=trace_id,
            session_key=session_key,
            user_id=user_id,
            model_id=model_id,
        ):
            yield ev


async def _stream_off_loop(
    prompt: str, *, trace_id: str, session_key: str, user_id: str, model_id: str
) -> AsyncIterator[dict[str, Any]]:
    """Run the whole turn on a dedicated worker thread (with its own event
    loop) and forward its events to the serving loop via a queue.

    The orchestrator turn is sync-heavy — the planner is a blocking LLM
    ``.invoke`` and bootstrap spawns specialist subprocesses. Driven directly
    on uvicorn's single serving loop, every blocking step freezes the whole
    server until it returns, so a browser switching conversations mid-turn just
    hangs. Running the turn off the serving loop keeps that loop free to serve
    other requests; the caller still holds ``_CONCURRENCY_GUARD`` for the
    worker's full lifetime, so turns stay serialised (they share
    ``.agent/runtime`` + process-global env)."""
    serving_loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    def _worker() -> None:
        async def _produce() -> None:
            try:
                async for ev in _run_streaming_locked(
                    prompt,
                    trace_id=trace_id,
                    session_key=session_key,
                    user_id=user_id,
                    model_id=model_id,
                ):
                    serving_loop.call_soon_threadsafe(queue.put_nowait, ev)
            except Exception as exc:  # noqa: BLE001  — never drop the stream
                log.exception("web: turn worker crashed")
                serving_loop.call_soon_threadsafe(
                    queue.put_nowait, {"type": "error", "message": str(exc)}
                )
                serving_loop.call_soon_threadsafe(
                    queue.put_nowait, {"type": "done", "text": ""}
                )
            finally:
                serving_loop.call_soon_threadsafe(queue.put_nowait, done)

        asyncio.run(_produce())

    thread = threading.Thread(target=_worker, name="web-turn", daemon=True)
    thread.start()
    try:
        while True:
            ev = await queue.get()
            if ev is done:
                break
            yield ev
    finally:
        # Hold the turn open until the worker (and its subprocesses) finish,
        # even if the client disconnected mid-stream — otherwise the guard would
        # release and let a second turn clobber the shared runtime/env.
        await asyncio.to_thread(thread.join)


async def _plan_and_dispatch(
    planner: Any,
    *,
    prompt: str,
    host: Any,
    router: Any,
    hmac_key: str,
    trace_id: str,
    history_context: str,
) -> AsyncIterator[dict[str, Any]]:
    """Stream the planner, then the reply — mirrors the CLI.

    If the planner answers in prose, its tokens ARE the reply and stream live
    token-by-token (the model's ``<think>`` scratch work is hidden by
    ``astream_plan``, same as the terminal). If it picks a capability, hand off
    to the specialist dispatch, which streams the specialist's ``thinking`` /
    ``tool_call`` / ``tool_result`` events plus the final text."""
    state = {"user_input": prompt, "trace_id": trace_id}
    astream = getattr(planner, "astream_plan", None)

    decision: Optional[dict] = None
    prose = ""
    if astream is not None:
        async for ev in astream(state):
            if ev.get("type") == "text":
                chunk = ev.get("chunk", "")
                prose += chunk
                yield {"type": "text", "chunk": chunk}
            elif ev.get("type") == "decision":
                decision = ev.get("decision")
    else:
        # Non-streaming planner (e.g. the mock/stub): no token streaming.
        decision = planner(state)

    decision = decision or {"capability": "", "response": ""}
    capability = (decision.get("capability") or "").strip()

    if not capability:
        # Prose answer. A streaming planner already emitted the tokens; a
        # non-streaming one hands over the whole thing here.
        if astream is None:
            prose = (decision.get("response") or "").strip()
            if prose:
                yield {"type": "text", "chunk": prose}
        yield {"type": "done", "text": prose.strip()}
        return

    # Capability -> specialist dispatch (A2A / MCP), which streams its own
    # thinking / tool_call / tool_result / text / done events.
    async for ev in dispatch_decision_stream(
        decision=decision,
        prompt=prompt,
        host=host,
        router=router,
        hmac_key=hmac_key,
        trace_id=trace_id,
        history_context=history_context,
    ):
        yield ev


async def _run_streaming_locked(
    prompt: str, *, trace_id: str, session_key: str, user_id: str, model_id: str
) -> AsyncIterator[dict[str, Any]]:
    import shutil

    from gateway import session_store
    from orchestrator.main import _bootstrap
    from orchestrator.mcp_host import MCPHost
    from orchestrator.router import CapabilityRouter

    prev_runtime = os.environ.get("LANGCHAIN_AGENT_RUNTIME_DIR")
    web_runtime = Path(".agent") / "runtime" / f"web-{os.getpid()}"

    hmac_key = secrets.token_urlsafe(32)
    host = MCPHost(hmac_key=hmac_key)
    router = CapabilityRouter()

    final_text = ""
    with _web_turn_env(user_id=user_id, model_id=model_id):
        os.environ["LANGCHAIN_AGENT_RUNTIME_DIR"] = str(web_runtime)
        try:
            history_context, full_context = _build_planner_context(session_key)
            await _bootstrap(host, router)
            planner = _build_planner(router, context_text=full_context)
            try:
                async for ev in _plan_and_dispatch(
                    planner,
                    prompt=prompt,
                    host=host,
                    router=router,
                    hmac_key=hmac_key,
                    trace_id=trace_id,
                    history_context=history_context,
                ):
                    if ev.get("type") == "text":
                        final_text += ev.get("chunk", "")
                    elif ev.get("type") == "done" and ev.get("text"):
                        final_text = ev["text"]
                    yield ev
            except Exception as exc:  # noqa: BLE001
                log.exception("web: planner/dispatch failed")
                yield {"type": "error", "message": f"planner: {exc}"}
                yield {"type": "done", "text": ""}
                return
        finally:
            await host.shutdown_all()
            if session_key and final_text:
                session_store.append(session_key, prompt, final_text)
            if prev_runtime is None:
                os.environ.pop("LANGCHAIN_AGENT_RUNTIME_DIR", None)
            else:
                os.environ["LANGCHAIN_AGENT_RUNTIME_DIR"] = prev_runtime
            shutil.rmtree(web_runtime, ignore_errors=True)
