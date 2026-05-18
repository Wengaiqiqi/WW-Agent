"""In-process bridge that lets the ``clarify`` tool ask the orchestrator's
user, even though tool-agent runs in a separate subprocess.

Flow:

  ReAct loop chooses clarify
    → wrapper calls ``await request(question, choices)``
       → pushes a ``clarify_request`` event onto the per-turn event queue
         (SSE generator picks it up, yields to orchestrator)
       → awaits a per-request asyncio.Future
  ...meanwhile, the orchestrator renders the question, gets the user's
     answer, POSTs it back to ``/a2a`` with ``skill_id=_clarify_response``
  ← the ``a2a_dispatch`` handler in ``agents.tool_agent.main`` calls
    ``resolve(request_id, answer)`` which sets the future
  → wrapper returns the answer string to the ReAct loop

The event queue is exposed via ``ContextVar`` so the wrapper signature
stays clean (``request(question, choices) -> str``) — Python's asyncio
contextvars propagate across ``asyncio.create_task`` so the wrapper
inherits the queue set by the SSE handler.

Why a queue rather than yielding directly from the generator: the
wrapper runs *inside* langchain's tool dispatch, several stack frames
below ``agent.run``'s generator. It can't ``yield`` from the outer
generator. The queue + driver-task pattern lets one async task pull
agent events and the wrapper push out-of-band events into the same
stream, with the SSE consumer pulling from the merged queue.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Per-task event queue. ``None`` outside an SSE streaming dispatch — the
# wrapper degrades gracefully in that case instead of hanging.
_event_queue: contextvars.ContextVar[Optional[asyncio.Queue[dict[str, Any]]]] = contextvars.ContextVar(
    "tool_agent_clarify_queue", default=None,
)

# request_id → future awaiting the user's answer. Module-level dict (one
# tool-agent subprocess = one ReAct loop at a time, so global is fine).
_pending: dict[str, asyncio.Future[str]] = {}

# How long to wait for the user to answer. Long enough that the user can
# read the question and think; short enough that a forgotten REPL doesn't
# pin the tool-agent forever on a single tool call.
_RESPONSE_TIMEOUT = 600.0  # 10 minutes


def set_event_queue(queue: "asyncio.Queue[dict[str, Any]]") -> None:
    """Called by the SSE handler before driving the ReAct loop."""
    _event_queue.set(queue)


async def request(question: str, choices: Optional[list[str]]) -> str:
    """Ask the orchestrator's user a clarifying question and return the answer.

    Returns a placeholder string instead of raising if no event queue is
    set in context (e.g. the LLM picked clarify via the synchronous MCP
    path, or someone called the wrapper from a unit test). The model
    receives a structured "unavailable" message and can decide what to do
    next — much better than blocking forever or crashing the turn.
    """
    queue = _event_queue.get()
    if queue is None:
        return "[clarify unavailable: this surface does not support interactive prompts]"

    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    _pending[request_id] = future

    await queue.put({
        "type": "clarify_request",
        "id": request_id,
        "question": question,
        "choices": list(choices) if choices else None,
    })

    try:
        answer = await asyncio.wait_for(future, timeout=_RESPONSE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("clarify request %s timed out after %.0fs", request_id, _RESPONSE_TIMEOUT)
        return "[clarify timed out — the user did not respond]"
    finally:
        _pending.pop(request_id, None)

    return answer or ""


def resolve(request_id: str, answer: str) -> bool:
    """Called by ``a2a_dispatch`` when orchestrator POSTs the user's answer.

    Returns True iff there was a pending future for ``request_id`` and the
    answer was delivered. False means the request was unknown (typo, late
    arrival after timeout, or duplicate response) — caller can treat that
    as a no-op.
    """
    future = _pending.get(request_id)
    if future is None or future.done():
        return False
    future.set_result(answer)
    return True


def _peek_pending() -> dict[str, Any]:
    """Test-only introspection of pending futures (do not call from product)."""
    return dict(_pending)
