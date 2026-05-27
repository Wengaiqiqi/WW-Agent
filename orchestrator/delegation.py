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
) -> AsyncIterator[dict[str, Any]]:
    """Mint the authz grant, build the task + meta, and yield the specialist's
    raw SSE events (thinking / tool_call / tool_result / text / done / error).

    Single source of truth for grant-minting; both the non-streaming
    ``delegate_via_a2a`` and the web bridge consume this."""
    if delegate is None:
        from orchestrator.a2a_client import delegate_task as delegate

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
) -> str:
    """Stream a ``tool.task`` / ``skill.<slug>`` and return the final text.

    Thin wrapper over :func:`delegate_via_a2a_stream` that collects ``text``
    deltas and returns when ``done`` arrives (or raises on ``error``)."""
    text_buffer = ""
    final_text = ""
    async for event in delegate_via_a2a_stream(
        capability=capability,
        arguments=arguments,
        user_input=user_input,
        hmac_key=hmac_key,
        trace_id=trace_id,
        permission_mode=permission_mode,
        history_context=history_context,
        delegate=delegate,
    ):
        etype = event.get("type", "")
        if etype == "text":
            text_buffer += event.get("chunk", "")
        elif etype == "done":
            final_text = event.get("text", "") or text_buffer
            break
        elif etype == "error":
            raise RuntimeError(event.get("message", "agent error"))
    return (final_text or text_buffer).strip()
