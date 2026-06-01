"""Tests for ToolAgentLoop streaming behavior.

The critical bug these guard against: consuming `agent.astream(...)` (an async
generator) with a synchronous `for` loop. The TypeError it raises only surfaces
in the SSE stream as a `{"type": "error"}` event, which is easy to miss in
end-to-end output.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents.tool_agent.agent_loop import ToolAgentLoop, _humanize_tool_echo


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
    # The terminal AIMessage in a real langgraph stream arrives via a `values`
    # snapshot at the end. Without it, terminal_answer_seen stays False and
    # the inconclusive-turn diagnostic gets appended. Include it so the
    # streamed text accurately reflects "two distinct messages, both reach
    # the UI, turn ended cleanly".
    terminal = AIMessage(content="第一段。", id="msg-B")

    agent = _FakeReactAgent(events=[
        ("messages", (a1, {})),
        ("messages", (b1, {})),
        ("values", {"messages": [HumanMessage(content="t"), terminal]}),
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


@pytest.mark.asyncio
async def test_run_dedupes_cumulative_chunks_across_rotating_message_ids():
    """Regression: providers that emit CUMULATIVE chunks (each chunk carries
    the full text-so-far) AND rotate ``msg.id`` between chunks were not caught
    by the per-message tracker, so the orchestrator's TUI rendered the answer
    once per chunk — the "成功获取... / 成功获取...── / 成功获取...──标题 / ..."
    stack-print symptom from real DeepSeek/Qwen runs."""
    c1 = AIMessage(content="成功获取。", id="msg-1")
    c2 = AIMessage(content="成功获取。内容是 P1003。", id="msg-2")
    c3 = AIMessage(content="成功获取。内容是 P1003。完毕。", id="msg-3")
    # Final terminal AIMessage as it would arrive via `values` mode after the
    # streaming chunks. Without it, terminal_answer_seen stays False and the
    # inconclusive-turn diagnostic gets appended on top.
    terminal = AIMessage(content="成功获取。内容是 P1003。完毕。", id="msg-3")

    agent = _FakeReactAgent(events=[
        ("messages", (c1, {})),
        ("messages", (c2, {})),
        ("messages", (c3, {})),
        ("values", {"messages": [HumanMessage(content="t"), terminal]}),
    ])

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = agent

    events = [e async for e in loop.run("t")]
    text_chunks = [e["chunk"] for e in events if e["type"] == "text"]

    assert text_chunks == ["成功获取。", "内容是 P1003。", "完毕。"], text_chunks


def test_humanize_tool_echo_rewrites_write_file_json():
    echo = '{"ok": true, "action": "create", "path": "D:\\\\ws\\\\3456.txt", "bytes": 2775}'
    out = _humanize_tool_echo(echo, [echo], task="保存为3456.txt")
    assert out.startswith("已保存到")
    assert "3456.txt" in out
    assert "2775 字节" in out
    assert "{" not in out  # no raw JSON leaks


def test_humanize_tool_echo_handles_mangled_windows_path_echo():
    """The real bug: the model echoes a Windows path with SINGLE backslashes,
    so its text is invalid JSON. We must still humanize it by matching against
    the (valid) tool result we returned, not by parsing the model's text."""
    import json as _json
    bs = chr(92)
    valid_result = _json.dumps(
        {"ok": True, "action": "create",
         "path": f"D:{bs}ws{bs}77777.txt", "bytes": 717},
    )
    # Model's echo: same blob but with the backslashes collapsed -> invalid JSON.
    mangled_echo = valid_result.replace(bs + bs, bs)
    with pytest.raises(_json.JSONDecodeError):
        _json.loads(mangled_echo)  # confirm the precondition

    out = _humanize_tool_echo(mangled_echo, [valid_result], task="保存为77777.txt")
    assert out.startswith("已保存到")
    assert "77777.txt" in out
    assert "717 字节" in out
    assert not out.lstrip().startswith("{")


def test_humanize_tool_echo_english_when_task_not_cjk():
    echo = '{"ok": true, "action": "update", "path": "/tmp/a.txt", "bytes": 10}'
    out = _humanize_tool_echo(echo, [echo], task="save it to a.txt")
    assert out == "Updated /tmp/a.txt (10 bytes)."


def test_humanize_tool_echo_leaves_natural_answer_untouched():
    answer = "已保存到 3456.txt，共 2775 字节。"
    assert _humanize_tool_echo(answer, [], task="保存为3456.txt") == answer


def test_humanize_tool_echo_leaves_unrelated_json_untouched():
    # JSON the user explicitly asked for — not a tool echo, no write signature.
    answer = '{"city": "Beijing", "temp": 21}'
    assert _humanize_tool_echo(answer, [], task="return the weather as JSON") == answer


def test_humanize_tool_echo_handles_trailing_characters():
    # Regression: the model appends junk after the echoed JSON object (a stray
    # period, "Done.", a code fence). The old endswith('}') check returned the
    # raw text unchanged here, leaking JSON; the leading-object extraction now
    # still recognises the echo and rewrites it.
    echo = '{"ok": true, "action": "create", "path": "a.txt", "bytes": 12}'
    for trailing in (".", "\n\nDone.", " ```"):
        out = _humanize_tool_echo(echo + trailing, [echo], task="save it to a.txt")
        assert out.startswith("Saved to"), repr(echo + trailing)
        assert "{" not in out


@pytest.mark.asyncio
async def test_run_humanizes_write_file_json_echo_in_done():
    """A model that ends the turn by pasting write_file's JSON return gets the
    final answer rewritten into a natural confirmation (the web UI renders
    done.text, so this is what the user sees)."""
    saved = '{"ok": true, "action": "create", "path": "D:\\\\ws\\\\3456.txt", "bytes": 2775}'
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[{"id": "1", "name": "write_file", "args": {"path": "3456.txt"}}],
    )
    tool_result_msg = ToolMessage(content=saved, tool_call_id="1", name="write_file")
    # The model echoes the tool JSON verbatim as its "answer".
    final_msg = AIMessage(content=saved)
    agent = _FakeReactAgent(events=[
        ("values", {"messages": [HumanMessage(content="保存为3456.txt"), tool_call_msg]}),
        ("values", {"messages": [
            HumanMessage(content="保存为3456.txt"), tool_call_msg, tool_result_msg, final_msg,
        ]}),
    ])

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = agent

    events = [e async for e in loop.run("保存为3456.txt")]
    done = [e for e in events if e["type"] == "done"]
    assert done
    assert done[-1]["text"].startswith("已保存到")
    assert "3456.txt" in done[-1]["text"]
    assert done[-1]["text"].lstrip()[0] != "{"


@pytest.mark.asyncio
async def test_run_withholds_streamed_json_echo_from_live_text():
    """When the model STREAMS a tool-JSON echo token-by-token as its final
    answer, none of it may reach the live `text` stream (CLI prints those
    verbatim). Instead the humanized confirmation is the only text emitted."""
    import json as _json
    bs = chr(92)
    valid = _json.dumps(
        {"ok": True, "action": "create", "path": f"D:{bs}ws{bs}9.txt", "bytes": 42},
    )
    mangled = valid.replace(bs + bs, bs)  # model collapses \\ -> \

    tool_call_msg = AIMessage(
        content="", tool_calls=[{"id": "1", "name": "write_file", "args": {"path": "9.txt"}}],
    )
    tool_result_msg = ToolMessage(content=valid, tool_call_id="1", name="write_file")
    final_msg = AIMessage(content=mangled, id="ans")

    # Stream the echo in 3 content chunks (same id), like a real provider,
    # then the values snapshot carrying the terminal content-only message.
    third = len(mangled) // 3
    c1 = AIMessage(content=mangled[:third], id="ans")
    c2 = AIMessage(content=mangled[:2 * third], id="ans")  # cumulative chunks
    c3 = AIMessage(content=mangled, id="ans")
    agent = _FakeReactAgent(events=[
        ("values", {"messages": [HumanMessage(content="保存为9.txt"), tool_call_msg]}),
        ("values", {"messages": [
            HumanMessage(content="保存为9.txt"), tool_call_msg, tool_result_msg]}),
        ("messages", (c1, {})),
        ("messages", (c2, {})),
        ("messages", (c3, {})),
        ("values", {"messages": [
            HumanMessage(content="保存为9.txt"), tool_call_msg, tool_result_msg, final_msg]}),
    ])

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = agent

    events = [e async for e in loop.run("保存为9.txt")]
    text_chunks = "".join(e["chunk"] for e in events if e["type"] == "text")
    # No raw JSON leaked to the live stream.
    assert "{" not in text_chunks and '"ok"' not in text_chunks, text_chunks
    # The humanized confirmation WAS painted (so the CLI isn't left blank).
    assert "已保存到" in text_chunks
    done = [e for e in events if e["type"] == "done"][-1]
    assert done["text"].startswith("已保存到")


@pytest.mark.asyncio
async def test_run_streams_json_answer_live_when_no_tool_was_called():
    """A `{`-leading answer with NO preceding tool call cannot be a tool echo,
    so it must stream live token-by-token (regression: the old check withheld
    EVERY JSON-leading answer, breaking streaming for a JSON answer the user
    legitimately asked for)."""
    answer = '{"city": "Beijing", "temp": 21}'
    third = len(answer) // 3
    c1 = AIMessage(content=answer[:third], id="ans")
    c2 = AIMessage(content=answer[:2 * third], id="ans")  # cumulative chunks
    c3 = AIMessage(content=answer, id="ans")
    final_msg = AIMessage(content=answer, id="ans")
    agent = _FakeReactAgent(events=[
        ("messages", (c1, {})),
        ("messages", (c2, {})),
        ("messages", (c3, {})),
        ("values", {"messages": [HumanMessage(content="weather as JSON"), final_msg]}),
    ])

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = agent

    events = [e async for e in loop.run("weather as JSON")]
    text_chunks = "".join(e["chunk"] for e in events if e["type"] == "text")
    # The JSON answer streamed live (it was NOT withheld) and reassembles whole.
    assert text_chunks == answer, text_chunks
    done = [e for e in events if e["type"] == "done"][-1]
    assert done["text"] == answer


@pytest.mark.asyncio
async def test_run_appends_diagnostic_when_no_terminal_answer_emitted():
    """Regression: when the model keeps calling tools without ever writing a
    plain text final answer, the turn previously ended in silence + divider.
    Now we synthesize a short diagnostic so the user understands the turn
    ended inconclusively rather than seeing a blank screen."""
    # Real providers stream the text portion of a "narration + tool_call"
    # AIMessage as a content-only chunk, with the tool_call arriving as a
    # separate tool_call_chunks event. Mirror that here so stream_buffer
    # actually accumulates the narration.
    narration_text_chunk = AIMessage(
        content="洛谷对直接抓取有反爬限制。我用搜索来获取题目信息。",
        id="m1",
    )
    tool_call_message = AIMessage(
        content="",
        id="m1",
        tool_calls=[{"id": "t1", "name": "web_extract", "args": {"url": "https://x"}}],
    )
    tool_result = ToolMessage(content="403 Forbidden", tool_call_id="t1", name="web_extract")
    # Crucially: no terminal AIMessage with content+no_tool_calls ever appears.
    agent = _FakeReactAgent(events=[
        ("messages", (narration_text_chunk, {})),
        ("values", {"messages": [HumanMessage(content="t"), tool_call_message]}),
        ("values", {"messages": [HumanMessage(content="t"), tool_call_message, tool_result]}),
    ])

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = agent

    events = [e async for e in loop.run("t")]
    done = [e for e in events if e["type"] == "done"]
    assert done, f"expected a done event: {events}"
    text = done[-1]["text"]
    assert "洛谷对直接抓取有反爬限制" in text
    # Diagnostic appended (look for any of the distinctive phrasings).
    assert "didn't reach" in text or "rephrase" in text, text
    # Critically: the diagnostic MUST also be emitted as a streamed text
    # event. The orchestrator's `_delegate_to_agent` only paints text events
    # to the screen; ``done.text`` alone is used for state recording, so a
    # diagnostic that lives only in ``done`` is invisible to the user.
    text_chunks = "".join(e["chunk"] for e in events if e["type"] == "text")
    assert "didn't reach" in text_chunks or "rephrase" in text_chunks, text_chunks


@pytest.mark.asyncio
async def test_run_yields_done_when_exception_after_answer_already_streamed():
    """Regression: when a late exception (recursion limit, transport hiccup)
    fires AFTER the model has already streamed a coherent answer, surface
    a clean ``done`` rather than ``error`` — otherwise the orchestrator's
    retry path re-runs the whole task and the user sees the answer twice.

    In a real langgraph stream, a `values` snapshot containing the terminal
    AIMessage fires before the exception, so ``terminal_answer_seen`` is True
    by the time we hit the except clause. Mirror that here.
    """

    terminal = AIMessage(content="The answer is 42.", id="m")

    class _LateBoom:
        def astream(self, *_a, **_k):
            async def _gen():
                yield ("messages", (terminal, {}))
                yield ("values", {"messages": [HumanMessage(content="t"), terminal]})
                raise RuntimeError("GraphRecursionError: limit hit at step 30")
                yield  # pragma: no cover
            return _gen()

    loop = ToolAgentLoop(llm=None, tools=[])
    loop._agent = _LateBoom()

    events = [e async for e in loop.run("t")]
    types = [e["type"] for e in events]
    assert "error" not in types, f"late exception leaked as error: {events}"
    assert types[-1] == "done", types
    assert events[-1]["text"] == "The answer is 42."


def test_prompt_for_state_without_context_emits_only_static_system_prompt():
    """No orchestrator context → exactly one SystemMessage, the static one.

    Regression target: appending an empty/whitespace context block as a
    second SystemMessage would silently inflate every tool-agent prompt
    with a meaningless "Recent turns: <none>" header.
    """
    from langchain_core.messages import HumanMessage as _HM, SystemMessage

    loop = ToolAgentLoop(llm=None, tools=[], context="   ")
    prompt = loop._prompt_for_state({"messages": [_HM(content="hi")]})

    sysmsgs = [m for m in prompt if isinstance(m, SystemMessage)]
    assert len(sysmsgs) == 1
    assert "workspace + web specialist" in sysmsgs[0].content
    # User message must still be present and last.
    assert isinstance(prompt[-1], _HM)


def test_prompt_for_state_with_context_appends_second_system_message():
    """Non-empty context → two SystemMessages; the second carries the
    orchestrator-supplied conversation snapshot. The peer needs to see
    referring-expression material *as background*, not as a user turn."""
    from langchain_core.messages import HumanMessage as _HM, SystemMessage

    context = (
        "User: 写一首诗\n"
        "orchestrator: 窗外是一棵老槐树。\n\n"
        "User: 保存到 a.txt"
    )
    loop = ToolAgentLoop(llm=None, tools=[], context=context)
    prompt = loop._prompt_for_state({"messages": [_HM(content="保存到 a.txt")]})

    sysmsgs = [m for m in prompt if isinstance(m, SystemMessage)]
    assert len(sysmsgs) == 2
    assert "workspace + web specialist" in sysmsgs[0].content
    # The conversation snapshot must appear verbatim in the second message
    # so the model can resolve 「上面的」/「这个」 in the user's task.
    assert "窗外是一棵老槐树。" in sysmsgs[1].content
    assert "Conversation context" in sysmsgs[1].content
    # Explicit guard against the model mistaking the snapshot for live
    # instructions to act on.
    assert "background" in sysmsgs[1].content.lower()


def test_prompt_for_state_omits_write_and_shell_under_read_only_toolset():
    """When the bound tool list excludes write/shell tools, the system
    prompt must also stop mentioning them — otherwise the model reads
    'you can run_command' and tries to call a tool that isn't bound."""
    from langchain_core.messages import HumanMessage as _HM, SystemMessage
    from langchain_core.tools import StructuredTool

    async def _noop(**_):  # pragma: no cover - tool body never runs in unit tests
        return ""

    # Construct the same read-only-mode tool set ``tools_for_mode`` would
    # produce — just enough to drive the prompt builder.
    read_only_tools = [
        StructuredTool(name=n, description=n, args_schema={
            "type": "object", "properties": {}, "required": [],
        }, coroutine=_noop)
        for n in ("read_file", "grep_search", "list_directory",
                  "web_search", "web_extract", "clarify")
    ]
    loop = ToolAgentLoop(llm=None, tools=read_only_tools)
    prompt = loop._prompt_for_state({"messages": [_HM(content="read X")]})

    sysmsgs = [m for m in prompt if isinstance(m, SystemMessage)]
    body = sysmsgs[0].content

    # Bound tools appear in the Tools section.
    assert "`read_file`" in body
    assert "`web_search`" in body
    # Unbound tools must NOT appear in the Tools section description list.
    # (They CAN appear in the "NOT available" mode-restriction note, which is
    # the desired behavior — it tells the model what's off-limits.)
    tools_section = body.split("## Environment")[0]
    assert "`run_command`" not in tools_section, tools_section
    assert "`write_file`" not in tools_section, tools_section
    # And the mode-restriction note is present, naming the unavailable tools.
    assert "Mode-restricted toolset" in body
    assert "run_command" in body  # somewhere — in the restriction note
    assert "write_file" in body
    # The pip-install hint is gone when run_command isn't bound.
    assert "pip install" not in body


def test_prompt_for_state_full_mode_includes_everything():
    """Default (no mode threaded) should still mention every tool — the
    legacy single-agent loop and unit tests rely on this."""
    from langchain_core.messages import HumanMessage as _HM, SystemMessage

    loop = ToolAgentLoop(llm=None, tools=[])  # empty → falls back to SYSTEM_PROMPT
    prompt = loop._prompt_for_state({"messages": [_HM(content="x")]})
    body = next(m for m in prompt if isinstance(m, SystemMessage)).content
    assert "`run_command`" in body
    assert "`write_file`" in body
    assert "`read_file`" in body
