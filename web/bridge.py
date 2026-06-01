"""Streaming bridge from the web surface to the orchestrator core.

Mirrors gateway.runner's bootstrap+dispatch but YIELDS events instead of
returning final text only. Reuses runner's helpers and shares its concurrency
guard so a web turn and an in-process gateway turn never run concurrently
(they share .agent/runtime files)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional

from gateway.runner import (
    _CONCURRENCY_GUARD,
    _build_planner,
    _build_planner_context,
    private_runtime_dir,
    scoped_env,
)
from web import config

log = logging.getLogger(__name__)


# How often to emit an SSE keepalive while waiting for the next real event.
# Must be comfortably below typical proxy/browser idle timeouts (~30-60s).
_HEARTBEAT_SECONDS = 10.0


# Process-wide cache of the STATIC capability catalog (capability names + tool
# schemas). The planner only needs these to decide routing — it never needs a
# live specialist. They're fixed by the agent cards on disk, so we snapshot
# them once (spawn → list → shut down) and reuse for every turn. This is what
# lets a prose turn skip the ~7s specialist bootstrap entirely; specialists
# are spawned lazily only when the planner actually picks a capability.
_CATALOG: dict[str, Any] | None = None


class _CatalogRouter:
    """Minimal duck-type of CapabilityRouter exposing just what
    ``gateway.runner._build_planner`` reads — so we can build the planner from
    the cached catalog without a live router / spawned specialists."""

    def __init__(self, capabilities: list[str], tool_schemas: dict[str, dict]):
        self._caps = capabilities
        self._schemas = tool_schemas

    def all_capabilities(self) -> list[str]:
        return self._caps

    def describe_tools(self) -> dict[str, dict]:
        return self._schemas


async def _capability_catalog() -> tuple[list[str], dict[str, dict]]:
    """Return the cached ``(capabilities, tool_schemas)`` catalog.

    On the first call, spawn the specialists once in an ISOLATED runtime dir,
    snapshot the router state, then tear them down. Subsequent calls hit the
    cache. Turns are serialised on ``_CONCURRENCY_GUARD`` so there's no race on
    the module global.
    """
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG["capabilities"], _CATALOG["tool_schemas"]

    import shutil
    import tempfile

    from orchestrator.main import _bootstrap
    from orchestrator.mcp_host import MCPHost
    from orchestrator.router import CapabilityRouter

    snap_dir = Path(tempfile.mkdtemp(prefix="ww-catalog-"))
    host = MCPHost(hmac_key=secrets.token_urlsafe(32))
    router = CapabilityRouter()
    try:
        with scoped_env({"LANGCHAIN_AGENT_RUNTIME_DIR": str(snap_dir)}):
            await _bootstrap(host, router)
            _CATALOG = {
                "capabilities": list(router.all_capabilities()),
                "tool_schemas": dict(router.describe_tools()),
            }
    finally:
        await host.shutdown_all()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return _CATALOG["capabilities"], _CATALOG["tool_schemas"]


async def warm_capability_catalog() -> None:
    """Pre-build the capability catalog at startup so the FIRST user turn
    doesn't pay the one-time specialist spawn (~10s before first token).

    Runs the spawn OFF the serving loop (its own loop in a worker thread) and
    UNDER the turn guard, so it neither freezes the event loop nor races a
    concurrent turn's env/runtime mutations. Best-effort: on failure the first
    turn just builds the catalog lazily."""
    try:
        async with _CONCURRENCY_GUARD:
            if _CATALOG is not None:
                return
            await asyncio.to_thread(lambda: asyncio.run(_capability_catalog()))
    except Exception:  # noqa: BLE001
        log.warning(
            "web: catalog warm-up failed; first turn will build it lazily",
            exc_info=True,
        )


def _user_workspace(user_id: str) -> Path:
    from agent_paths import config_dir

    safe = user_id or "anon"
    ws = config_dir() / "web" / "workspaces" / safe
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@contextlib.contextmanager
def _web_turn_env(
    *, user_id: str, model_id: str,
    base_url: str = "", api_key: str = "", protocol: str = "",
) -> Iterator[Path]:
    """Set the per-turn env (memory user, forced workspace-write, per-user
    workspace root, selected model, and — for custom endpoints — base_url /
    api_key / protocol) and restore the prior values on exit. Delegates the
    snapshot/restore to ``gateway.runner.scoped_env`` (shared with the gateway
    turn path)."""
    ws = _user_workspace(user_id)
    with scoped_env({
        "LANGCHAIN_AGENT_MEMORY_USER": user_id or None,
        "LANGCHAIN_AGENT_PERMISSION_MODE": config.WEB_PERMISSION_MODE,
        "LANGCHAIN_AGENT_WORKSPACE_ROOT": str(ws),
        "LANGCHAIN_AGENT_MODEL": model_id or None,
        "LANGCHAIN_AGENT_BASE_URL": base_url or None,
        "LANGCHAIN_AGENT_API_KEY": api_key or None,
        "LANGCHAIN_AGENT_PROTOCOL": protocol or None,
    }):
        yield ws


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
    base_url: str = "",
    api_key: str = "",
    protocol: str = "",
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
            base_url=base_url,
            api_key=api_key,
            protocol=protocol,
        ):
            yield ev


async def _stream_off_loop(
    prompt: str,
    *, trace_id: str, session_key: str, user_id: str, model_id: str,
    base_url: str = "", api_key: str = "", protocol: str = "",
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

    def _emit(item: Any) -> None:
        """Push an item to the serving loop's queue, tolerating a loop that has
        already been torn down (full server shutdown) — nobody to receive."""
        try:
            serving_loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            pass

    def _worker() -> None:
        async def _produce() -> None:
            try:
                async for ev in _run_streaming_locked(
                    prompt,
                    trace_id=trace_id,
                    session_key=session_key,
                    user_id=user_id,
                    model_id=model_id,
                    base_url=base_url,
                    api_key=api_key,
                    protocol=protocol,
                ):
                    _emit(ev)
            except asyncio.CancelledError:
                # Turn aborted mid-stream — the browser disconnected (closed
                # the tab, switched conversations) and Starlette cancelled the
                # SSE response. Expected, not a crash: log quietly and let the
                # finally signal done so the consuming side unblocks. The
                # ``async for`` already ran ``_run_streaming_locked``'s finally
                # (host.shutdown_all, env restore) during unwinding.
                log.info("web: turn cancelled mid-stream (client disconnected)")
            except Exception as exc:  # noqa: BLE001  — never drop the stream
                log.exception("web: turn worker crashed")
                _emit({"type": "error", "message": str(exc)})
                _emit({"type": "done", "text": ""})
            finally:
                _emit(done)

        # CancelledError is a BaseException, not Exception — if it ever escapes
        # _produce (e.g. raised during loop shutdown), keep it from crashing the
        # worker thread with a noisy traceback.
        try:
            asyncio.run(_produce())
        except asyncio.CancelledError:
            pass

    thread = threading.Thread(target=_worker, name="web-turn", daemon=True)
    thread.start()
    # Open the stream with an immediate keepalive so the connection carries
    # bytes from the start. The first real token can be 10s+ away (specialist
    # spawn on the first turn, or a slow LLM behind a proxy), and an idle SSE
    # connection through a system HTTP proxy that doesn't bypass localhost gets
    # dropped — which surfaces server-side as "client disconnected" and the
    # user sees no reply. Keepalives are ignored by the client's SSE parser.
    yield {"type": "keepalive"}
    try:
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield {"type": "keepalive"}
                continue
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
    ensure_specialists: Any,
    hmac_key: str,
    trace_id: str,
    history_context: str,
) -> AsyncIterator[dict[str, Any]]:
    """Stream the planner, then the reply — mirrors the CLI.

    If the planner answers in prose, its tokens ARE the reply and stream live
    token-by-token (the model's ``<think>`` scratch work is hidden by
    ``astream_plan``, same as the terminal). No specialist is spawned in this
    path. If it picks a capability, ``ensure_specialists()`` is awaited to
    lazily bootstrap the MCP host + router, then dispatch streams the
    specialist's ``thinking`` / ``tool_call`` / ``tool_result`` events plus the
    final text.

    ``ensure_specialists`` is an async callable returning ``(host, router)``;
    it is invoked ONLY when a capability is chosen, which is the whole point of
    lazy spawn — a prose turn never touches it."""
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
        # non-streaming one hands over the whole thing here. No spawn.
        if astream is None:
            prose = (decision.get("response") or "").strip()
            if prose:
                yield {"type": "text", "chunk": prose}
        yield {"type": "done", "text": prose.strip()}
        return

    # Capability -> spawn the specialists now (lazy), then dispatch (A2A / MCP),
    # which streams its own thinking / tool_call / tool_result / text / done.
    host, router = await ensure_specialists()
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
    prompt: str,
    *, trace_id: str, session_key: str, user_id: str, model_id: str,
    base_url: str = "", api_key: str = "", protocol: str = "",
) -> AsyncIterator[dict[str, Any]]:
    from gateway import session_store

    hmac_key = secrets.token_urlsafe(32)
    # Lazily-created MCP host/router — only a capability turn pays the spawn.
    host: Any = None
    router: Any = None

    async def ensure_specialists() -> tuple[Any, Any]:
        """Spawn specialists on first capability use; memoised for the turn."""
        nonlocal host, router
        from orchestrator.main import _bootstrap
        from orchestrator.mcp_host import MCPHost
        from orchestrator.router import CapabilityRouter

        if host is None:
            host = MCPHost(hmac_key=hmac_key)
            router = CapabilityRouter()
            await _bootstrap(host, router)
        return host, router

    final_text = ""
    with _web_turn_env(user_id=user_id, model_id=model_id, base_url=base_url,
                       api_key=api_key, protocol=protocol), \
            private_runtime_dir("web"):
        try:
            history_context, full_context = _build_planner_context(session_key)
            # Build the planner from the cached static catalog — no spawn here.
            capabilities, tool_schemas = await _capability_catalog()
            planner = _build_planner(
                _CatalogRouter(capabilities, tool_schemas),
                context_text=full_context,
            )
            try:
                async for ev in _plan_and_dispatch(
                    planner,
                    prompt=prompt,
                    ensure_specialists=ensure_specialists,
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
            # Only shut down if a capability turn actually spawned the host.
            if host is not None:
                await host.shutdown_all()
            if session_key and final_text:
                session_store.append(session_key, prompt, final_text)
