"""Tests for the clarify bridge that lets the multi-agent tool-agent ask
the orchestrator's user mid-turn.

Covers:
- Round-trip: put + resolve completes the future with the answer
- Graceful fallback when no event queue is set in context
- Pending registry hygiene: pending dict is empty after success and timeout
- ``resolve`` returns False for unknown request_ids
- Timeout returns a structured fallback string instead of hanging the loop
"""
from __future__ import annotations

import asyncio

import pytest

from agents.tool_agent import clarify_bridge


@pytest.mark.asyncio
async def test_round_trip_resolve_returns_answer():
    queue: asyncio.Queue = asyncio.Queue()
    clarify_bridge.set_event_queue(queue)

    async def _request_then_let_caller_resolve():
        return await clarify_bridge.request("color?", ["red", "blue"])

    task = asyncio.create_task(_request_then_let_caller_resolve())
    # Pull the clarify_request event off the queue.
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert event["type"] == "clarify_request"
    assert event["question"] == "color?"
    assert event["choices"] == ["red", "blue"]
    rid = event["id"]

    # Simulate the orchestrator POSTing the user's answer.
    assert clarify_bridge.resolve(rid, "red") is True

    answer = await asyncio.wait_for(task, timeout=1.0)
    assert answer == "red"
    # Pending registry is clean afterwards.
    assert rid not in clarify_bridge._peek_pending()


@pytest.mark.asyncio
async def test_no_event_queue_returns_structured_message():
    """Outside an SSE handler the bridge degrades to a structured string so
    the model gets a defined response rather than hanging."""
    # Reset the ContextVar by entering a fresh task with no setter.
    async def _run_no_queue():
        return await clarify_bridge.request("anything?", None)

    answer = await asyncio.create_task(_run_no_queue())
    assert "clarify unavailable" in answer.lower()


@pytest.mark.asyncio
async def test_resolve_unknown_request_id_returns_false():
    assert clarify_bridge.resolve("does-not-exist", "ignored") is False


@pytest.mark.asyncio
async def test_resolve_after_completion_is_idempotent_no_op():
    queue: asyncio.Queue = asyncio.Queue()
    clarify_bridge.set_event_queue(queue)

    task = asyncio.create_task(clarify_bridge.request("?", None))
    event = await queue.get()
    rid = event["id"]

    # First resolve completes the future.
    assert clarify_bridge.resolve(rid, "answer-1") is True
    assert await task == "answer-1"

    # Second resolve sees the future is done (and entry was popped).
    assert clarify_bridge.resolve(rid, "answer-2") is False


@pytest.mark.asyncio
async def test_timeout_returns_fallback_and_cleans_pending(monkeypatch):
    """When the user never answers we don't hang the ReAct loop forever —
    return a structured timeout string and drop the pending future."""
    monkeypatch.setattr(clarify_bridge, "_RESPONSE_TIMEOUT", 0.05)

    queue: asyncio.Queue = asyncio.Queue()
    clarify_bridge.set_event_queue(queue)

    task = asyncio.create_task(clarify_bridge.request("late?", None))
    event = await queue.get()
    rid = event["id"]

    answer = await asyncio.wait_for(task, timeout=1.0)
    assert "timed out" in answer.lower()
    # No leaked entries.
    assert rid not in clarify_bridge._peek_pending()


@pytest.mark.asyncio
async def test_resolve_handles_empty_answer():
    """``answer=""`` (user Ctrl+C'd the prompt) must still satisfy the future
    and return an empty string — not block waiting for a "real" answer."""
    queue: asyncio.Queue = asyncio.Queue()
    clarify_bridge.set_event_queue(queue)

    task = asyncio.create_task(clarify_bridge.request("?", None))
    event = await queue.get()
    rid = event["id"]
    assert clarify_bridge.resolve(rid, "") is True

    answer = await asyncio.wait_for(task, timeout=1.0)
    assert answer == ""
