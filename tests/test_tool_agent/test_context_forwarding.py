"""Tool-agent context forwarding.

Plan-A bug fix: the orchestrator now passes ``context`` (a snapshot of the
recent conversation) on every A2A delegation. These tests pin the two
forwarding hops on the receiving side:

  payload["context"] → _run_agent_streaming(context=...) → ToolAgentLoop(context=...)

Both hops are one-line forwards; if either regresses, referring expressions
like 「上面的作文」 silently lose their referent again.
"""
from __future__ import annotations

import pytest

import agents.tool_agent.main as tool_agent_main


class _FakeAgent:
    def __init__(self, *, captured: dict):
        self._captured = captured

    async def run(self, *, task: str):
        # Echo back what the loop was constructed with so the test can assert
        # the context made it all the way through.
        yield {"type": "done", "text": self._captured.get("context", "")}


@pytest.mark.asyncio
async def test_run_agent_streaming_passes_context_to_tool_agent_loop(monkeypatch):
    """``_run_agent_streaming(task, context=X)`` must reach ``ToolAgentLoop(context=X)``."""
    captured: dict = {}

    def _fake_tool_agent_loop(*, llm, tools, context=""):
        captured["context"] = context
        return _FakeAgent(captured=captured)

    async def _fake_get_llm():
        return object()

    def _fake_make_tools(mode="danger-full-access"):
        captured["mode"] = mode
        return []

    monkeypatch.setattr(tool_agent_main, "ToolAgentLoop", _fake_tool_agent_loop)
    monkeypatch.setattr(tool_agent_main, "get_llm", _fake_get_llm)
    monkeypatch.setattr(tool_agent_main, "make_langchain_tools", _fake_make_tools)

    events = []
    async for event in tool_agent_main._run_agent_streaming(
        "save 上面的作文 to a.txt", context="User: 写一首诗\norchestrator: 老槐树。",
    ):
        events.append(event)

    assert captured["context"] == "User: 写一首诗\norchestrator: 老槐树。"
    # And the streaming still produces a well-formed done event.
    assert any(e["type"] == "done" for e in events), events


@pytest.mark.asyncio
async def test_run_agent_streaming_defaults_context_to_empty(monkeypatch):
    """Backward compatibility: omitting ``context`` means an empty string,
    not ``None`` (which would crash ``ToolAgentLoop.__init__`` on ``.strip()``)."""
    captured: dict = {}

    def _fake_tool_agent_loop(*, llm, tools, context=""):
        captured["context"] = context
        return _FakeAgent(captured=captured)

    async def _fake_get_llm():
        return object()

    monkeypatch.setattr(tool_agent_main, "ToolAgentLoop", _fake_tool_agent_loop)
    monkeypatch.setattr(tool_agent_main, "get_llm", _fake_get_llm)
    monkeypatch.setattr(
        tool_agent_main, "make_langchain_tools",
        lambda mode="danger-full-access": [],
    )

    async for _ in tool_agent_main._run_agent_streaming("just a task"):
        pass

    assert captured["context"] == ""


@pytest.mark.asyncio
async def test_run_agent_streaming_forwards_permission_mode(monkeypatch):
    """The grant-derived permission_mode must reach ``make_langchain_tools``
    so read-only delegations never get ``write_file`` / ``run_command`` bound."""
    captured: dict = {}

    def _fake_tool_agent_loop(*, llm, tools, context=""):
        return _FakeAgent(captured=captured)

    async def _fake_get_llm():
        return object()

    def _fake_make_tools(mode="danger-full-access"):
        captured["mode"] = mode
        return []

    monkeypatch.setattr(tool_agent_main, "ToolAgentLoop", _fake_tool_agent_loop)
    monkeypatch.setattr(tool_agent_main, "get_llm", _fake_get_llm)
    monkeypatch.setattr(tool_agent_main, "make_langchain_tools", _fake_make_tools)

    async for _ in tool_agent_main._run_agent_streaming(
        "read README", context="", permission_mode="read-only",
    ):
        pass

    assert captured["mode"] == "read-only"
