"""Shared A2A task-delegation entry point.

``tool.task`` and ``skill.<slug>`` are *agent-level* capabilities: the planner
routes to them, but they are NOT MCP tools (tool-agent's MCP server exposes
``read_file`` / ``grep_search`` / … but no ``tool.task``). They must be driven
over the A2A streaming endpoint instead.

Three callers need this exact logic — the REPL controller, the chat-platform
gateway (``gateway.runner``), and the one-shot ``cli.py prompt`` path
(``orchestrator.turns.TurnRunner``). Keeping it in one place is what stops the
"this entry point forgot to wire A2A and fell back to the MCP path, which
fails with `unknown tool: tool.task`" class of bug from recurring (it has bitten
the gateway once and the one-shot path once).
"""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from orchestrator.permission_gate import PermissionGate

# Signature of the streaming delegate: yields event dicts (text / done / error).
DelegateFn = Callable[..., AsyncIterator[dict[str, Any]]]


async def delegate_via_a2a_stream(
    *,
    capability: str,
    arguments: dict | None,
    user_input: str,
    hmac_key: str,
    trace_id: str,
    permission_mode: str,
    history_context: str = "",
    delegate: DelegateFn | None = None,
    runtime_dir: Path | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Mint the authz grant, build the task + meta, and yield the specialist's
    raw SSE events (thinking / tool_call / tool_result / text / done / error).

    Single source of truth for grant-minting; both the non-streaming
    ``delegate_via_a2a`` and the web bridge consume this.

    ``runtime_dir`` selects which peers.json the real delegate discovers the
    peer from — pass the per-turn dir (``host.runtime_dir``) when the host was
    bootstrapped into one, so the web bridge doesn't read a foreign peers.json
    from the shared default dir. It is bound onto the real ``delegate_task``;
    an injected ``delegate`` (tests) keeps its own narrower signature."""
    if delegate is None:
        from orchestrator.a2a_client import delegate_task
        delegate = partial(delegate_task, runtime_dir=runtime_dir)

    arguments = arguments or {}
    gate = PermissionGate(mode=permission_mode, hmac_key=hmac_key, trace_id=trace_id)

    if capability == "tool.task":
        peer_id = "tool-agent"
        task_text = arguments.get("task", user_input)
        grant = gate.sign(target_specialist="tool-agent", tool="tool.task")
        meta = {
            "trace_id": trace_id,
            "agent_caller": "orchestrator",
            "permission_mode": permission_mode,
            "authz_grant": grant,
        }
    else:  # skill.<slug>
        peer_id = "skill-agent"
        slug = capability[len("skill."):]
        if arguments:
            task_text = (
                f"{user_input}\n\n[Planner arguments] "
                + json.dumps(arguments, ensure_ascii=False)
            )
        else:
            task_text = user_input
        grant = gate.sign(target_specialist="skill-agent", tool=capability)
        meta = {
            "trace_id": trace_id,
            "agent_caller": "orchestrator",
            "permission_mode": permission_mode,
            "skill_slug": slug,
            "authz_grant": grant,
        }

    async for event in delegate(
        peer_id=peer_id, task=task_text, meta=meta, context=history_context,
    ):
        yield event


async def delegate_via_a2a(
    *,
    capability: str,
    arguments: dict | None,
    user_input: str,
    hmac_key: str,
    trace_id: str,
    permission_mode: str,
    history_context: str = "",
    delegate: DelegateFn | None = None,
    runtime_dir: Path | None = None,
) -> str:
    """Stream a ``tool.task`` / ``skill.<slug>`` and return the final text.

    Thin wrapper over :func:`delegate_via_a2a_stream` that collects ``text``
    deltas and returns when ``done`` arrives (or raises on ``error``).

    ``runtime_dir`` is forwarded to the stream so the real delegate discovers
    peers from the per-turn dir (see :func:`delegate_via_a2a_stream`)."""
    text_buffer = ""
    final_text = ""
    saw_done = False
    async for event in delegate_via_a2a_stream(
        capability=capability,
        arguments=arguments,
        user_input=user_input,
        hmac_key=hmac_key,
        trace_id=trace_id,
        permission_mode=permission_mode,
        history_context=history_context,
        delegate=delegate,
        runtime_dir=runtime_dir,
    ):
        etype = event.get("type", "")
        if etype == "text":
            text_buffer += event.get("chunk", "")
        elif etype == "done":
            final_text = event.get("text", "") or text_buffer
            saw_done = True
            break
        elif etype == "error":
            raise RuntimeError(event.get("message", "agent error"))
    # If the stream ended without a `done` event the peer crashed mid-reply.
    # Returning the partial text would let the orchestrator treat truncated
    # output as authoritative; surface it as a failure instead.
    if not saw_done:
        raise RuntimeError(
            f"{capability} stream ended without a done event "
            f"(peer crashed mid-reply; got {len(text_buffer)} chars)"
        )
    return (final_text or text_buffer).strip()
