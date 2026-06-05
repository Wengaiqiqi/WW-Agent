"""Cancellation must survive a LangGraph runtime that *wraps* the node's
``asyncio.CancelledError`` in another exception.

The MCP dispatch node calls ``host.call_tool``; if that raises
``CancelledError`` (Ctrl-C / turn abort), ``TurnRunner.run`` must propagate it
as ``CancelledError`` so the REPL controller calls ``host.cancel_all()``.
Older LangGraph let the bare CancelledError bubble through ``graph.ainvoke``;
newer runtimes can surface it as a plain ``Exception`` with the CancelledError
in its ``__cause__`` chain — which used to be swallowed into a
``TurnResult(error=...)`` (observed only on CI, where deps float to latest).
Hardening: walk the exception chain and re-raise as cancellation.
"""
from __future__ import annotations

import asyncio

import pytest

import orchestrator.turns as turns
from orchestrator.turns import TurnRunner, _is_cancellation


def test_is_cancellation_detects_bare():
    assert _is_cancellation(asyncio.CancelledError()) is True


def test_is_cancellation_detects_wrapped_via_cause():
    try:
        try:
            raise asyncio.CancelledError()
        except asyncio.CancelledError as ce:
            raise RuntimeError("runtime wrapped it") from ce
    except RuntimeError as exc:
        assert _is_cancellation(exc) is True


def test_is_cancellation_detects_wrapped_via_context():
    try:
        try:
            raise asyncio.CancelledError()
        except asyncio.CancelledError:
            raise RuntimeError("implicit context")  # no `from`
    except RuntimeError as exc:
        assert _is_cancellation(exc) is True


def test_is_cancellation_false_for_plain_error():
    assert _is_cancellation(RuntimeError("boom")) is False
    assert _is_cancellation(ValueError("x")) is False


class _Router:
    def all_capabilities(self):
        return ["read_file"]

    def resolve(self, capability):
        return "tool-agent"

    def describe_tools(self):
        return {}


class _WrappingGraph:
    """Mimics a langgraph runtime that re-raises a node's CancelledError
    wrapped in a non-Cancelled exception."""

    async def ainvoke(self, state):
        try:
            raise asyncio.CancelledError()
        except asyncio.CancelledError as ce:
            raise RuntimeError("cancelled by runtime") from ce


@pytest.mark.asyncio
async def test_turn_runner_propagates_wrapped_cancellation(monkeypatch):
    monkeypatch.setattr(turns, "build_graph", lambda **kw: _WrappingGraph())

    runner = TurnRunner(
        host=object(),
        router=_Router(),
        hmac_key="k",
        permission_mode_provider=lambda: "danger-full-access",
        # Plain (non-LLMPlanner) planner pins an MCP capability so run() takes
        # the graph path; fast_route is skipped for non-LLMPlanner planners.
        planner=lambda _state: {"capability": "read_file"},
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run("read something", trace_id="t1")
