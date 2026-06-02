"""Streaming bridge from the web surface to the orchestrator core.

Mirrors gateway.runner's bootstrap+dispatch but YIELDS events instead of
returning final text only. Reuses runner's helpers and shares its concurrency
guard so a web turn and an in-process gateway turn never run concurrently
(they share .agent/runtime files)."""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from gateway.runner import (
    _build_planner,
    _build_planner_context,
    scoped_env,
)
from orchestrator.turn_context import TurnContext
from web import config
from web.turn_loop import TurnLoop

log = logging.getLogger(__name__)


# How often to emit an SSE keepalive while waiting for the next real event.
# Must be comfortably below typical proxy/browser idle timeouts (~30-60s).
_HEARTBEAT_SECONDS = 10.0


# Bounded concurrency for web turns. Default 1 reproduces the old single-guard
# behavior; WEB_MAX_CONCURRENCY>1 lets independent turns run in parallel now
# that per-turn state lives on the TurnContext (per-user workspace, per-turn-id
# runtime dir, explicit spawn env) instead of process-global os.environ.
_TURN_SEMAPHORE = asyncio.Semaphore(config.max_concurrency())

# Guards the one-time _CATALOG build. Cross-THREAD (not asyncio): turns run on
# worker threads each with their own event loop, and the startup warm-up runs on
# yet another thread, so the catalog snapshot must be serialized with a plain
# threading lock rather than an event-loop-bound asyncio.Lock.
_CATALOG_LOCK = threading.Lock()


# The single persistent loop that owns the pool and runs every turn coroutine.
# Lazily started on the first turn (and by warm-up) so importing the module is
# side-effect-free for tests that monkeypatch _stream_off_loop.
_TURN_LOOP = TurnLoop()
_POOL: Any = None          # orchestrator.specialist_pool.SpecialistPool | None
_POOL_LOCK = threading.Lock()


def _ensure_turn_loop() -> TurnLoop:
    if not _TURN_LOOP.is_running:
        _TURN_LOOP.start()
    return _TURN_LOOP


async def _host_factory(*, signature, runtime_dir, hmac_key):
    """Cold-spawn a bootstrapped (host, router) for a pool signature. Runs ON the
    turn loop. The signature's fields are reconstructed into a turn_env via a
    throwaway TurnContext so the spawn env matches the per-turn path exactly."""
    from orchestrator.main import _bootstrap
    from orchestrator.mcp_host import MCPHost
    from orchestrator.router import CapabilityRouter

    user_id, workspace_root, model_id, base_url, api_key, protocol = signature
    ctx = TurnContext(
        turn_id="pool", user_id=user_id, workspace_root=Path(workspace_root),
        permission_mode=config.WEB_PERMISSION_MODE, model_id=model_id,
        base_url=base_url, api_key=api_key, protocol=protocol,
        session_key="", trace_id="pool", hmac_key=hmac_key,
        runtime_dir=runtime_dir,
    )
    host = MCPHost(hmac_key=hmac_key, turn_env=ctx.turn_env())
    router = CapabilityRouter()
    await _bootstrap(host, router)
    return host, router


def _get_pool() -> Any:
    """Lazily build the process-wide pool (thread-safe). The pool object itself
    is loop-agnostic to *create*; only its coroutines must run on the turn loop."""
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            from orchestrator.specialist_pool import SpecialistPool
            _POOL = SpecialistPool(
                factory=_host_factory,
                max_hosts=config.pool_max_hosts(),
                idle_ttl=config.pool_idle_ttl(),
            )
    return _POOL


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
    cache. The build is guarded by ``_CATALOG_LOCK`` (double-checked) so that
    when WEB_MAX_CONCURRENCY>1 lets several turns run at once, only one of them
    pays the spawn and there's no race on the module global.
    """
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG["capabilities"], _CATALOG["tool_schemas"]

    import shutil
    import tempfile

    from orchestrator.main import _bootstrap
    from orchestrator.mcp_host import MCPHost
    from orchestrator.router import CapabilityRouter

    # Cross-thread guard: re-check under the lock (another turn/the warm-up may
    # have just built it) so the heavy spawn happens exactly once.
    with _CATALOG_LOCK:
        if _CATALOG is not None:
            return _CATALOG["capabilities"], _CATALOG["tool_schemas"]

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

    Runs the spawn OFF the serving loop (its own loop in a worker thread) so it
    doesn't freeze the event loop. The catalog build is itself serialized on
    ``_CATALOG_LOCK``, so a turn that arrives mid-warm-up blocks on that lock
    rather than racing the global. Best-effort: on failure the first turn just
    builds the catalog lazily."""
    try:
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


def _web_turn_context(
    *, user_id: str, model_id: str, session_key: str, trace_id: str,
    base_url: str = "", api_key: str = "", protocol: str = "",
) -> TurnContext:
    """Build the per-turn context for a web turn: the server-enforced
    workspace-write tier, a per-user workspace root, the selected model and —
    for custom endpoints — base_url / api_key / protocol, plus a per-turn-id
    runtime-discovery dir so parallel turns don't collide on peers.json.

    Single definition of "what a web turn's env is": ``ctx.turn_env()`` is both
    the in-process scope and the subprocess overlay."""
    turn_id = secrets.token_hex(8)
    return TurnContext(
        turn_id=turn_id,
        user_id=user_id,
        workspace_root=_user_workspace(user_id),
        permission_mode=config.WEB_PERMISSION_MODE,
        model_id=model_id, base_url=base_url, api_key=api_key, protocol=protocol,
        session_key=session_key, trace_id=trace_id,
        hmac_key=secrets.token_urlsafe(32),
        runtime_dir=Path(".agent") / "runtime" / f"web-{turn_id}",
    )


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

    Bounded by ``_TURN_SEMAPHORE`` (WEB_MAX_CONCURRENCY; default 1 = serialized).
    Builds the per-turn TurnContext, bootstraps a private MCPHost, runs the
    planner, streams the dispatch, then appends the final pair to session_store
    (planner context). The route layer is responsible for persisting to SQLite
    for the UI."""
    if not prompt or not prompt.strip():
        yield {"type": "done", "text": ""}
        return

    async with _TURN_SEMAPHORE:
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
    """Run the whole turn on the shared persistent ``TurnLoop`` and forward its
    events to the serving loop via a queue.

    The orchestrator turn is sync-heavy — the planner is a blocking LLM
    ``.invoke`` and bootstrap spawns specialist subprocesses. Driven directly
    on uvicorn's single serving loop, every blocking step freezes the whole
    server until it returns, so a browser switching conversations mid-turn just
    hangs. Running the turn off the serving loop keeps that loop free to serve
    other requests; the caller still holds a ``_TURN_SEMAPHORE`` slot for the
    turn's full lifetime, so at most WEB_MAX_CONCURRENCY turns run at once.

    Why one PERSISTENT loop (not a fresh thread per turn): pooled ``MCPHost``s
    hold stdio transports bound to the loop that created them, so every turn
    that touches a pooled host must run on that one loop. Each turn still carries
    its own per-turn-id runtime dir and explicit spawn env (TurnContext), so
    concurrent turns on the shared loop never collide on ``.agent/runtime`` or
    process-global env."""
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
            # Turn aborted mid-stream — the browser disconnected (closed the
            # tab, switched conversations) and the turn was cancelled on the
            # turn loop. Expected, not a crash. The ``async for`` already ran
            # ``_run_streaming_locked``'s finally (lease release / host
            # shutdown, runtime-dir cleanup) during unwinding. Re-raise so the
            # task records as cancelled.
            log.info("web: turn cancelled mid-stream (client disconnected)")
            raise
        except Exception as exc:  # noqa: BLE001  — never drop the stream
            log.exception("web: turn worker crashed")
            _emit({"type": "error", "message": str(exc)})
            _emit({"type": "done", "text": ""})
        finally:
            _emit(done)

    # Create the coroutine ON the turn loop (loop-affine: it may touch pooled
    # hosts). run_coroutine_factory returns a concurrent.futures.Future.
    turn_loop = _ensure_turn_loop()
    future = turn_loop.run_coroutine_factory(_produce)
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
        # Hold the turn open until the turn coroutine (and its subprocesses)
        # finish, even if the client disconnected mid-stream — its finally
        # releases the lease / shuts the host down before we free the semaphore
        # slot. On disconnect, cancel the turn on its own loop first.
        # run_coroutine_threadsafe's future stays PENDING until the task
        # completes, so cancel() reliably propagates to the asyncio task.
        if not future.done():
            future.cancel()
        try:
            await asyncio.to_thread(future.result)
        except BaseException:  # noqa: BLE001 — cancelled or already surfaced
            pass


async def _plan_and_dispatch(
    planner: Any,
    *,
    prompt: str,
    ensure_specialists: Any,
    hmac_key: Any,  # str | Callable[[], str] — late-bound for pooled hosts
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
        # hmac_key may be a late-bound getter (pooled host's baked key is only
        # known after ensure_specialists()); resolve it here, post-spawn.
        hmac_key=hmac_key() if callable(hmac_key) else hmac_key,
        trace_id=trace_id,
        history_context=history_context,
    ):
        yield ev


async def _run_streaming_locked(
    prompt: str,
    *, trace_id: str, session_key: str, user_id: str, model_id: str,
    base_url: str = "", api_key: str = "", protocol: str = "",
) -> AsyncIterator[dict[str, Any]]:
    import shutil

    from gateway import session_store

    ctx = _web_turn_context(
        user_id=user_id, model_id=model_id, base_url=base_url, api_key=api_key,
        protocol=protocol, session_key=session_key, trace_id=trace_id,
    )

    # Specialists are obtained lazily — only a capability turn pays for them; a
    # prose turn never calls ensure_specialists. When pooling is enabled the host
    # is leased from (and returned to) the warm pool and dispatch uses the host's
    # baked hmac_key. When disabled we cold-spawn a private host and shut it down
    # in the finally — byte-for-byte today's behavior, using ctx.hmac_key.
    lease: Any = None
    host: Any = None
    router: Any = None
    dispatch_hmac_key = ctx.hmac_key

    async def ensure_specialists() -> tuple[Any, Any]:
        """Lease a pooled host (enabled) or cold-spawn a private one (disabled);
        memoised for the turn."""
        nonlocal lease, host, router, dispatch_hmac_key
        if config.pool_enabled():
            if lease is None:
                lease = await _get_pool().acquire(ctx)
                dispatch_hmac_key = lease.hmac_key  # host's baked key, not ctx's
            return lease.host, lease.router
        from orchestrator.main import _bootstrap
        from orchestrator.mcp_host import MCPHost
        from orchestrator.router import CapabilityRouter

        if host is None:
            host = MCPHost(hmac_key=ctx.hmac_key, turn_env=ctx.turn_env())
            router = CapabilityRouter()
            await _bootstrap(host, router)
        return host, router

    final_text = ""
    # Scope the per-turn env in-process (for the planner config + memory
    # snapshot); the subprocess channel is the host's turn_env above.
    with scoped_env(ctx.turn_env()):
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
                    # Late-bound: ensure_specialists sets the host's baked key
                    # (pooled path) before any dispatch; prose turns never use it.
                    hmac_key=lambda: dispatch_hmac_key,
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
            # Pooled path: return the host to the warm pool (not shut down).
            if lease is not None:
                await _get_pool().release(lease)
            # Disabled path: a capability turn cold-spawned a private host.
            if host is not None:
                await host.shutdown_all()
            if session_key and final_text:
                session_store.append(session_key, prompt, final_text)
            shutil.rmtree(ctx.runtime_dir, ignore_errors=True)
