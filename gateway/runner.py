"""Bridge from the gateway adapters to the orchestrator.

Adapters call :func:`run_turn` with the user's plaintext prompt. It boots the
orchestrator (the same way ``orchestrator.main.run_prompt`` does), runs a
single turn, and returns the final assistant text -- without going through the
TUI mux. Each call is fully isolated: MCP children are spawned and shut down
per turn, so there is no shared session state between platform messages.

Capability dispatch matches the multi-agent REPL's three branches:
    1. planner returns no capability -> use its prose ``response`` directly
    2. planner returns ``tool.task`` or ``skill.<slug>`` -> A2A-stream delegate
       to the specialist (this is a separate code path from MCP-tool calls;
       tool-agent's MCP server has no ``tool.task`` tool registered)
    3. planner returns a simple MCP capability (``calculator`` etc.) -> let
       :class:`TurnRunner` route it via the LangGraph MCP path

Before this module, the gateway only had path 3 wired, so any time the
planner picked ``tool.task`` (e.g. "report your working directory" -> ``pwd``)
the gateway crashed with ``unknown tool: tool.task`` from tool-agent's MCP
server.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import secrets
import shutil
from pathlib import Path
from typing import Iterator, Optional

from orchestrator.main import _bootstrap, _build_orchestrator_llm
from orchestrator.mcp_host import MCPHost
from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux
from orchestrator.turns import LLMPlanner, _stub_planner, TurnRunner

log = logging.getLogger(__name__)


def max_concurrency() -> int:
    """Max simultaneous gateway turns. Default 1 = today's serialized behavior
    (reversible rollout); raise ``GATEWAY_MAX_CONCURRENCY`` to enable multi-user
    parallelism now that per-turn state lives on the TurnContext (per-user
    memory, per-turn-id runtime dir, explicit planner cfg)."""
    try:
        return max(1, int(os.environ.get("GATEWAY_MAX_CONCURRENCY", "1")))
    except ValueError:
        return 1


# Bounded gateway concurrency. Default 1 reproduces the old single asyncio-lock
# behavior; GATEWAY_MAX_CONCURRENCY>1 lets independent turns run in parallel.
# Rebound at runtime by ``set_max_concurrency`` (the /gateway Start prompt).
_CURRENT_MAX: int = max_concurrency()
_GATEWAY_SEMAPHORE = asyncio.Semaphore(_CURRENT_MAX)


def current_max_concurrency() -> int:
    """The concurrency limit in effect right now (env default until the user
    overrides it via ``set_max_concurrency``). Used as the prompt default."""
    return _CURRENT_MAX


def set_max_concurrency(n: int) -> int:
    """Set the process-wide gateway concurrency to ``max(1, n)`` and return the
    effective value.

    Rebinds this module's asyncio semaphore (in-loop callers: REPL / QQ) and
    best-effort the feishu_ws threading semaphore (cross-thread Feishu SDK
    dispatch). Both ``async with _GATEWAY_SEMAPHORE`` and ``with _dispatch_sem``
    read their module global per call, so only turns started after this call
    see the new limit; in-flight turns hold the old object and finish normally.

    The feishu_ws fan-out is wrapped defensively: importing it is safe (lark is
    imported lazily inside its functions, not at module load), but a concurrency
    update must never be what breaks gateway start, so any unexpected failure is
    logged and swallowed."""
    global _GATEWAY_SEMAPHORE, _CURRENT_MAX
    n = max(1, int(n))
    _CURRENT_MAX = n
    _GATEWAY_SEMAPHORE = asyncio.Semaphore(n)
    try:
        from gateway import feishu_ws
        feishu_ws.set_dispatch_limit(n)
    except Exception:  # noqa: BLE001 - never let a concurrency update block start
        log.debug("set_max_concurrency: could not update feishu_ws limit", exc_info=True)
    return n


@contextlib.contextmanager
def scoped_env(values: dict[str, Optional[str]]) -> Iterator[None]:
    """Set/clear process env vars for the duration of the block, restoring the
    prior values (set or unset) on exit. A value of ``None`` clears the var.

    Single owner for the per-turn env snapshot/restore logic shared by the
    gateway and the web bridge. They pass different key sets — the web
    custom-endpoint path also scopes base_url/api_key/protocol — but the
    snapshot/restore mechanism (and any fix to it) lives here once.
    """
    prev = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def private_runtime_dir(prefix: str) -> Iterator[Path]:
    """Point ``LANGCHAIN_AGENT_RUNTIME_DIR`` at a per-PID dir for the block, then
    restore the prior value and remove the dir.

    Isolates each process's specialist discovery files (peers.json + the
    ``<id>.a2a-url`` sidecars) so a gateway and a REPL sharing the project cwd
    don't clobber each other's ``peers.json`` (which produced "All connection
    attempts failed" on the other's next tool-task)."""
    rt = Path(".agent") / "runtime" / f"{prefix}-{os.getpid()}"
    with scoped_env({"LANGCHAIN_AGENT_RUNTIME_DIR": str(rt)}):
        try:
            yield rt
        finally:
            shutil.rmtree(rt, ignore_errors=True)


async def run_turn(
    prompt: str,
    *,
    trace_id: Optional[str] = None,
    session_key: str = "",
    user_id: str = "",
) -> str:
    """Run one orchestrator turn and return the assistant's text reply.

    Empty/blank prompts short-circuit with an empty reply so platform
    adapters can safely forward whatever the user typed.

    ``session_key`` (when non-empty) keys the conversation memory in
    :mod:`gateway.session_store`. Recent history is loaded before the turn
    and surfaced to both the planner (via the LLMPlanner ``context_provider``)
    and the A2A specialists (via the ``context`` parameter of delegate_task).
    The new user/assistant pair is appended after the turn completes.

    ``user_id`` (when non-empty) scopes the ``memory`` tool to a per-user
    directory so multi-user chat platforms keep each person's facts separate.
    See :mod:`tool.tool_memory` for the on-disk layout. Empty user_id falls
    back to the global ``memories/`` layout.

    Concurrency: bounded by ``_GATEWAY_SEMAPHORE`` (GATEWAY_MAX_CONCURRENCY;
    default 1 = serialized). Each turn carries its own TurnContext — per-user
    memory, per-turn-id ``.agent/runtime`` dir, and an explicitly-resolved
    planner cfg — so concurrent turns no longer collide on shared runtime files
    or process-global env.
    """
    if not prompt or not prompt.strip():
        return ""

    async with _GATEWAY_SEMAPHORE:
        return await _run_turn_locked(
            prompt,
            trace_id=trace_id or "gw1",
            session_key=session_key,
            user_id=user_id,
        )


def _build_planner(router: CapabilityRouter, *, context_text: str = "", cfg=None):
    """Return either an LLMPlanner (preferred) or the stub planner.

    ``context_text`` is rendered into the planner's "Session context" slot
    via the ``context_provider`` closure. The stub planner ignores it. ``cfg``
    (a resolved ActiveConfig from ``resolve_config(ctx)``) is the per-turn model
    selection — preferred over the process-global ``LANGCHAIN_AGENT_MODEL`` env
    so concurrent turns don't share a planner config.
    """
    provider = cfg.provider if cfg is not None else os.environ.get("LANGCHAIN_AGENT_MODEL", "")
    if (provider or "").startswith("mock"):
        return _stub_planner
    try:
        llm = _build_orchestrator_llm(cfg)
        return LLMPlanner(
            llm=llm,
            available_capabilities=router.all_capabilities(),
            context_provider=(lambda _t=context_text: _t),
            tool_schemas=router.describe_tools(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "gateway: could not build LLM planner (%s); falling back to stub. "
            "Run /model in the REPL once to configure a provider.",
            exc,
        )
        return _stub_planner


async def _delegate_via_a2a(
    *,
    capability: str,
    decision: dict,
    user_input: str,
    hmac_key: str,
    trace_id: str,
    history_context: str = "",
    permission_mode: Optional[str] = None,
) -> str:
    """Stream a task to tool-agent or skill-agent via A2A and return the text.

    Thin adapter over :func:`orchestrator.delegation.delegate_via_a2a` (the
    single source of truth shared with the REPL controller and the one-shot
    ``cli.py prompt`` path). ``permission_mode`` comes from the per-turn
    TurnContext; it falls back to the env default only when not supplied (legacy
    callers), so concurrent turns don't share it via process-global env.
    """
    from orchestrator.delegation import delegate_via_a2a

    if permission_mode is None:
        permission_mode = os.environ.get(
            "LANGCHAIN_AGENT_PERMISSION_MODE", "workspace-write"
        )
    return await delegate_via_a2a(
        capability=capability,
        arguments=decision.get("arguments") or {},
        user_input=user_input,
        hmac_key=hmac_key,
        trace_id=trace_id,
        permission_mode=permission_mode,
        history_context=history_context,
    )


def _build_planner_context(session_key: str, *, memory_user: str = "") -> tuple[str, str]:
    """Return ``(history_context_for_a2a, full_context_for_planner)``.

    * ``history_context_for_a2a``: just the recent-conversation block; the
      A2A specialists get it as their referring-expression background.
    * ``full_context_for_planner``: history + persistent ``memory`` snapshot,
      injected into the planner's "Session context" slot so prose answers
      can reference saved facts ("what's my name?").

    ``memory_user`` scopes the memory snapshot to that user explicitly (from the
    per-turn TurnContext) instead of the process-global ``LANGCHAIN_AGENT_MEMORY_USER``
    env, so concurrent turns read the right user's memory.
    """
    from gateway import session_store

    history = session_store.load(session_key) if session_key else []
    history_context = session_store.format_for_prompt(history) if history else ""
    try:
        from tool.tool_memory import snapshot_for_system_prompt

        memory_snapshot = snapshot_for_system_prompt(user=memory_user or None) or ""
    except Exception:  # noqa: BLE001
        memory_snapshot = ""
    parts = [p for p in (memory_snapshot, history_context) if p]
    return history_context, "\n\n".join(parts)


async def _drive_telemetry_tail(mux: StreamMux):
    """Start the telemetry tail task and return a cleanup callback."""
    from orchestrator import telemetry

    telemetry.reset_log()
    stop = asyncio.Event()
    tail_task = asyncio.create_task(telemetry.tail(mux, stop))

    async def _stop() -> None:
        stop.set()
        try:
            await asyncio.wait_for(tail_task, timeout=2.0)
        except asyncio.TimeoutError:
            tail_task.cancel()
            try:
                await tail_task
            except asyncio.CancelledError:
                pass

    return _stop


async def _dispatch_decision(
    *,
    decision: dict,
    prompt: str,
    host: MCPHost,
    router: CapabilityRouter,
    hmac_key: str,
    trace_id: str,
    history_context: str,
    permission_mode: Optional[str] = None,
) -> str:
    """Drive the right dispatch path based on the planner's decision.

    Three branches, matching the multi-agent REPL: prose answer, A2A
    delegation (tool.task / skill.<slug>), or simple MCP capability.
    """
    capability = (decision.get("capability") or "").strip()

    # Branch A: planner answered in prose, no dispatch needed.
    if not capability:
        return (decision.get("response") or "").strip()

    # Branch B: A2A delegation -- tool-agent or skill-agent does a ReAct loop
    # and streams back the final text.
    if capability == "tool.task" or capability.startswith("skill."):
        try:
            return await _delegate_via_a2a(
                capability=capability,
                decision=decision,
                user_input=prompt,
                hmac_key=hmac_key,
                trace_id=trace_id,
                history_context=history_context,
                permission_mode=permission_mode,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("gateway: A2A delegate failed")
            return f"[error] {capability}: {exc}"

    # Branch C: simple MCP capability (``calculator``, ``current_datetime``,
    # etc.). TurnRunner does the LangGraph MCP dispatch; we hand it the
    # planner's decision via a pinned single-call planner so it doesn't
    # re-plan and pick something else.
    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key=hmac_key,
        permission_mode_provider=(
            lambda _pm=permission_mode: _pm if _pm is not None
            else os.environ.get("LANGCHAIN_AGENT_PERMISSION_MODE", "workspace-write")
        ),
        planner=lambda _state, _d=decision: _d,
    )
    try:
        result = await runner.run(prompt, trace_id=trace_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("gateway: MCP dispatch failed")
        return f"[error] {capability}: {exc}"
    if result.error:
        return f"[error] {result.error}"
    return (result.text or "").strip()


async def _run_turn_locked(
    prompt: str,
    *,
    trace_id: str,
    session_key: str = "",
    user_id: str = "",
) -> str:
    """Orchestrator-bootstrap-and-dispatch core. Caller holds the lock."""
    from dataclasses import replace

    from gateway import session_store
    from orchestrator.turn_context import TurnContext

    # Build the explicit per-turn context. The gateway's permission default is
    # workspace-write (matching the spawn-time default and what _dispatch_decision
    # / _delegate_via_a2a assume), NOT TurnContext.from_env's danger-full-access
    # CLI default — so override it explicitly. The runtime dir is keyed per
    # turn_id (was per-PID) so parallel turns can't collide.
    ctx = TurnContext.from_env(
        session_key=session_key, trace_id=trace_id,
        hmac_key=secrets.token_urlsafe(32),
    )
    ctx = replace(
        ctx,
        user_id=user_id,
        permission_mode=os.environ.get(
            "LANGCHAIN_AGENT_PERMISSION_MODE", "workspace-write"
        ),
        runtime_dir=Path(".agent") / "runtime" / f"gw-{ctx.turn_id}",
    )

    # Resolve the planner config from the context (not process-global env), so
    # two concurrent turns can't clobber each other's model/endpoint selection.
    from config import resolve_config

    cfg = resolve_config(ctx)

    host = MCPHost(hmac_key=ctx.hmac_key, turn_env=ctx.turn_env())
    router = CapabilityRouter()
    # Mux receives streaming output during the turn but we discard it --
    # only the final assistant text matters here.
    mux = StreamMux(out=io.StringIO())

    reply_text = ""
    is_slash_command = False
    stop_tail = None

    # No scoped_env: per-turn config travels explicitly — cfg into the planner,
    # ctx.user_id into the memory snapshot, ctx.permission_mode into dispatch,
    # and ctx.turn_env() into the host for subprocesses. Nothing reads per-turn
    # state off process-global env, so concurrent turns stay isolated.
    try:
        history_context, full_context = _build_planner_context(
            session_key, memory_user=ctx.user_id,
        )

        await _bootstrap(host, router)

        # Slash commands (/task /chat /peers /help) for whitelisted users.
        # A string reply short-circuits the planner; None falls through to
        # normal chat. comm.* tools are available because _bootstrap spawned
        # the comm-agent onto this per-turn host.
        from gateway.slash import handle_slash
        slash_reply = await handle_slash(
            prompt, host=host, session_key=session_key, user_id=user_id,
        )
        if slash_reply is not None:
            is_slash_command = True
            reply_text = slash_reply
            return reply_text

        planner = _build_planner(router, context_text=full_context, cfg=cfg)
        stop_tail = await _drive_telemetry_tail(mux)

        try:
            decision = planner({"user_input": prompt, "trace_id": trace_id})
        except Exception as exc:  # noqa: BLE001
            log.exception("gateway: planner failed")
            reply_text = f"[error] planner: {exc}"
            return reply_text

        reply_text = await _dispatch_decision(
            decision=decision,
            prompt=prompt,
            host=host,
            router=router,
            hmac_key=ctx.hmac_key,
            trace_id=trace_id,
            history_context=history_context,
            permission_mode=ctx.permission_mode,
        )
        return reply_text
    finally:
        if stop_tail is not None:
            await stop_tail()
        await host.shutdown_all()
        # Persist the turn even when the reply was an error -- a future turn
        # might still want to refer to it. Slash commands are operator
        # actions / remote conversations, not local chat, so they are
        # deliberately excluded from the planner's history.
        if session_key and reply_text and not is_slash_command:
            session_store.append(session_key, prompt, reply_text)
        # Best-effort cleanup of this turn's private discovery dir.
        shutil.rmtree(ctx.runtime_dir, ignore_errors=True)
