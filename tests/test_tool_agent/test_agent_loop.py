"""Tests for ToolAgentLoop streaming behavior.

The critical bug these guard against: consuming `agent.astream(...)` (an async
generator) with a synchronous `for` loop. The TypeError it raises only surfaces
in the SSE stream as a `{"type": "error"}` event, which is easy to miss in
end-to-end output.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents.tool_agent.agent_loop import ToolAgentLoop


class _FakeReactAgent:
    """Stand-in for langgraph's create_react_agent that emits a scripted stream."""

    def __init__(self, events: list[tuple[str, object]]):
        self._events = events

    def astream(self, _input, *, config=None, stream_mode=None):
        events = list(self._events)

        async def _gen():
            for event in events:
                yield event

        return _gen()


@pytest.mark.asyncio
async def test_run_consumes_astream_with_async_for():
    """Regression: must use `async for` over astream — sync `for` raises
    'async_generator' object is not iterable."""
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[{"id": "1", "name": "read_file", "args": {"path": "x.txt"}}],
    )
    tool_result_msg = ToolMessage(content="hello world", tool_call_id="1", name="read_file")
    final_msg = AIMessage(content="The file says hello world.")

    fake_text_chunk = AIMessage(content="Reading the file...")
    agent = _FakeReactAgent(events=[
        ("messages", (fake_text_chunk, {})),
        ("values", {"messages": [HumanMessage(content="task"), tool_call_msg]}),
        ("values", {"messages": [
            HumanMessage(content="task"), tool_call_msg, tool_result_msg, final_msg,
        ]}),
    ])

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = agent  # bypass _build_agent

    events = []
    async for event in loop.run("task"):
        events.append(event)

    types = [e["type"] for e in events]
    assert types[0] == "thinking"
    assert "text" in types, f"text streaming missing: {types}"
    assert "tool_call" in types, f"tool_call missing: {types}"
    assert "tool_result" in types, f"tool_result missing: {types}"
    assert types[-1] == "done"
    # And critically: no error event leaked from sync-iter-over-async-gen.
    assert not any(e["type"] == "error" for e in events), events


@pytest.mark.asyncio
async def test_run_dedupes_tool_call_and_result_across_values_snapshots():
    """Regression: stream_mode='values' yields the WHOLE message list at every
    state update, so the same tool_call/ToolMessage shows up in N successive
    snapshots. We must only surface each one once — otherwise the orchestrator
    TUI redraws the same `⏺ tool` header and result 2-3 times per call."""
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[{"id": "call_42", "name": "write_file", "args": {"path": "a.txt"}}],
    )
    tool_result_msg = ToolMessage(
        content="ok", tool_call_id="call_42", name="write_file",
    )
    final_msg = AIMessage(content="done")

    # The same tool_call & ToolMessage appear in successive values snapshots,
    # exactly as langgraph emits them.
    agent = _FakeReactAgent(events=[
        ("values", {"messages": [HumanMessage(content="t"), tool_call_msg]}),
        ("values", {"messages": [HumanMessage(content="t"), tool_call_msg, tool_result_msg]}),
        ("values", {"messages": [
            HumanMessage(content="t"), tool_call_msg, tool_result_msg, final_msg,
        ]}),
    ])

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = agent

    events = [e async for e in loop.run("t")]
    types = [e["type"] for e in events]

    assert types.count("tool_call") == 1, f"tool_call duplicated: {types}"
    assert types.count("tool_result") == 1, f"tool_result duplicated: {types}"
    assert types[-1] == "done"


@pytest.mark.asyncio
async def test_run_dedupes_cumulative_streaming_chunks():
    """Regression: some providers (DeepSeek flash variants, certain local proxies)
    stream CUMULATIVE chunks — every chunk carries the full assistant message so
    far. Without dedup, every chunk re-emits everything already on screen, so the
    TUI shows the sentence repeated dozens of times."""
    # Same .id across chunks — that's how langchain marks chunks of one message.
    c1 = AIMessage(content="好的，", id="msg-1")
    c2 = AIMessage(content="好的，我先看看", id="msg-1")
    c3 = AIMessage(content="好的，我先看看文件。", id="msg-1")

    agent = _FakeReactAgent(events=[
        ("messages", (c1, {})),
        ("messages", (c2, {})),
        ("messages", (c3, {})),
        ("values", {"messages": [
            HumanMessage(content="t"),
            AIMessage(content="好的，我先看看文件。", id="msg-1"),
        ]}),
    ])

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = agent

    events = [e async for e in loop.run("t")]
    text_chunks = [e["chunk"] for e in events if e["type"] == "text"]

    # Three cumulative chunks → exactly three non-overlapping deltas.
    assert text_chunks == ["好的，", "我先看看", "文件。"], text_chunks
    assert "".join(text_chunks) == "好的，我先看看文件。"


@pytest.mark.asyncio
async def test_run_dedupes_identical_re_emitted_chunk():
    """Regression: langgraph occasionally re-emits an identical chunk back-to-back
    when the agent node transitions. We must collapse the verbatim repeat."""
    c1 = AIMessage(content="好的，我已经读完了。", id="msg-7")
    c1_repeat = AIMessage(content="好的，我已经读完了。", id="msg-7")

    agent = _FakeReactAgent(events=[
        ("messages", (c1, {})),
        ("messages", (c1_repeat, {})),
        ("messages", (c1_repeat, {})),
        ("values", {"messages": [
            HumanMessage(content="t"),
            AIMessage(content="好的，我已经读完了。", id="msg-7"),
        ]}),
    ])

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = agent

    events = [e async for e in loop.run("t")]
    text_chunks = [e["chunk"] for e in events if e["type"] == "text"]

    assert text_chunks == ["好的，我已经读完了。"], text_chunks


@pytest.mark.asyncio
async def test_run_treats_different_message_ids_independently():
    """Two AIMessages with different ids must not share dedup state — chunks
    from the second message that happen to start the same as the first must
    NOT be swallowed."""
    a1 = AIMessage(content="第一段。", id="msg-A")
    b1 = AIMessage(content="第一段。", id="msg-B")  # same content, new id

    agent = _FakeReactAgent(events=[
        ("messages", (a1, {})),
        ("messages", (b1, {})),
        ("values", {"messages": [HumanMessage(content="t")]}),
    ])

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = agent

    events = [e async for e in loop.run("t")]
    text_chunks = [e["chunk"] for e in events if e["type"] == "text"]

    assert text_chunks == ["第一段。", "第一段。"], text_chunks


@pytest.mark.asyncio
async def test_run_surfaces_inner_exception_as_error_event():
    class _Boom:
        def astream(self, *_a, **_k):
            async def _gen():
                raise RuntimeError("upstream blew up")
                yield  # pragma: no cover
            return _gen()

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = _Boom()

    events = [e async for e in loop.run("task")]
    assert events[0] == {"type": "thinking"}
    assert events[-1]["type"] == "error"
    assert "upstream blew up" in events[-1]["message"]
