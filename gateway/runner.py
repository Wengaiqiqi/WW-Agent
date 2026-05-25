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
import io
import logging
import os
import secrets
from typing import Optional

from orchestrator.main import _bootstrap, _build_orchestrator_llm
from orchestrator.mcp_host import MCPHost
from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux
from orchestrator.turns import LLMPlanner, _stub_planner, TurnRunner

log = logging.getLogger(__name__)


_CONCURRENCY_GUARD = asyncio.Lock()


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

    Concurrency: one turn at a time per process. The orchestrator and its
    spawned MCP children rely on ``.agent/runtime/`` files that are not safe
    for concurrent writers; serialising turns sidesteps that.
    """
    if not prompt or not prompt.strip():
        return ""

    async with _CONCURRENCY_GUARD:
        return await _run_turn_locked(
            prompt,
            trace_id=trace_id or "gw1",
            session_key=session_key,
            user_id=user_id,
        )


def _build_planner(router: CapabilityRouter, *, context_text: str = ""):
    """Return either an LLMPlanner (preferred) or the stub planner.

    ``context_text`` is rendered into the planner's "Session context" slot
    via the ``context_provider`` closure. The stub planner ignores it.
    """
    provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
    if provider.startswith("mock"):
        return _stub_planner
    try:
        llm = _build_orchestrator_llm()
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
) -> str:
    """Stream a task to tool-agent or skill-agent via A2A and return the text.

    Thin adapter over :func:`orchestrator.delegation.delegate_via_a2a` (the
    single source of truth shared with the REPL controller and the one-shot
    ``cli.py prompt`` path). Reads the gateway's permission mode from env and
    forwards the planner's ``arguments``.
    """
    from orchestrator.delegation import delegate_via_a2a

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


def _apply_memory_user_env(user_id: str) -> None:
    """Set / unset the per-user memory scope env var. Subprocesses inherit it."""
    if user_id:
        os.environ["LANGCHAIN_AGENT_MEMORY_USER"] = user_id
    elif "LANGCHAIN_AGENT_MEMORY_USER" in os.environ:
        del os.environ["LANGCHAIN_AGENT_MEMORY_USER"]


def _restore_memory_user_env(prev: Optional[str]) -> None:
    """Undo whatever ``_apply_memory_user_env`` did, idempotently."""
    if prev is None:
        os.environ.pop("LANGCHAIN_AGENT_MEMORY_USER", None)
    else:
        os.environ["LANGCHAIN_AGENT_MEMORY_USER"] = prev


def _build_planner_context(session_key: str) -> tuple[str, str]:
    """Return ``(history_context_for_a2a, full_context_for_planner)``.

    * ``history_context_for_a2a``: just the recent-conversation block; the
      A2A specialists get it as their referring-expression background.
    * ``full_context_for_planner``: history + persistent ``memory`` snapshot,
      injected into the planner's "Session context" slot so prose answers
      can reference saved facts ("what's my name?").
    """
    from gateway import session_store

    history = session_store.load(session_key) if session_key else []
    history_context = session_store.format_for_prompt(history) if history else ""
    try:
        from tool.tool_memory import snapshot_for_system_prompt

        memory_snapshot = snapshot_for_system_prompt() or ""
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
        permission_mode_provider=lambda: os.environ.get(
            "LANGCHAIN_AGENT_PERMISSION_MODE", "workspace-write"
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
    from pathlib import Path

    from gateway import session_store

    # Snapshot env BEFORE the try so the finally has a consistent baseline
    # even if anything below raises.
    prev_user_env = os.environ.get("LANGCHAIN_AGENT_MEMORY_USER")

    # Snapshot peers.json BEFORE bootstrap. The gateway's per-turn
    # MCPHost.spawn writes its subprocesses' a2a URLs to the shared
    # .agent/runtime/peers.json -- which is exactly the file the REPL's
    # ``delegate_task`` reads later to find ITS OWN tool-agent. After the
    # gateway turn we kill our subprocesses, but peers.json keeps pointing
    # at the dead URLs -- the REPL then fails with "All connection
    # attempts failed" on the next /tools or tool-task request. Save now,
    # restore in finally.
    peers_path = Path(".agent/runtime/peers.json")
    saved_peers: Optional[str] = None
    try:
        if peers_path.exists():
            saved_peers = peers_path.read_text(encoding="utf-8")
    except OSError:
        saved_peers = None

    hmac_key = secrets.token_urlsafe(32)
    host = MCPHost(hmac_key=hmac_key)
    router = CapabilityRouter()
    # Mux receives streaming output during the turn but we discard it --
    # only the final assistant text matters here.
    mux = StreamMux(out=io.StringIO())

    reply_text = ""
    is_slash_command = False
    stop_tail = None
    try:
        # CRITICAL: apply the per-user memory scope env BEFORE building the
        # planner context. ``_build_planner_context`` reads the memory file
        # via ``snapshot_for_system_prompt``, which keys off this env var
        # to pick the right user directory. Doing it the other way around
        # made Branch A (prose answers) see the global / previous user's
        # memory.
        _apply_memory_user_env(user_id)
        history_context, full_context = _build_planner_context(session_key)

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

        planner = _build_planner(router, context_text=full_context)
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
            hmac_key=hmac_key,
            trace_id=trace_id,
            history_context=history_context,
        )
        return reply_text
    finally:
        if stop_tail is not None:
            await stop_tail()
        await host.shutdown_all()
        # Restore peers.json to whatever the REPL bootstrap left there
        # (or remove it entirely if there wasn't one to begin with). See
        # the "Snapshot peers.json" comment above for why this matters.
        try:
            if saved_peers is not None:
                peers_path.write_text(saved_peers, encoding="utf-8")
            elif peers_path.exists():
                peers_path.unlink()
        except OSError:
            pass
        # Persist the turn even when the reply was an error -- a future turn
        # might still want to refer to it ("you said you couldn't do that").
        # Slash commands are operator actions / remote conversations, not local
        # chat, so they are deliberately excluded from the planner's history.
        if session_key and reply_text and not is_slash_command:
            session_store.append(session_key, prompt, reply_text)
        # Restore the env var the way we found it; without this a subsequent
        # REPL turn in the same process would inherit the gateway's scoping.
        _restore_memory_user_env(prev_user_env)
