# tests/test_orchestrator/test_llm_planner.py
import pytest
from types import SimpleNamespace

from orchestrator.turns import LLMPlanner
from agents.shared.mock_chat_model import MockChatModel


def test_llm_planner_emits_structured_decision():
    llm = MockChatModel(
        responses=['{"capability": "read_file", "arguments": {"path": "README.md"}}']
    )
    planner = LLMPlanner(
        llm=llm,
        available_capabilities=["read_file", "skill.baidu-ecommerce-search"],
    )
    decision = planner({"user_input": "read the readme", "trace_id": "t"})
    assert decision["capability"] == "read_file"
    assert decision["arguments"]["path"] == "README.md"


def test_llm_planner_strips_code_fences():
    llm = MockChatModel(
        responses=['```json\n{"capability": "read_file", "arguments": {"path": "x"}}\n```']
    )
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    decision = planner({"user_input": "read x", "trace_id": "t"})
    assert decision["capability"] == "read_file"


def test_llm_planner_returns_conversational_response():
    llm = MockChatModel(
        responses=['{"capability": "", "response": "Hello, how can I help?"}']
    )
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    decision = planner({"user_input": "hello", "trace_id": "t"})
    assert decision["capability"] == ""
    assert "Hello" in decision["response"]


def test_llm_planner_raises_on_empty_llm_response():
    llm = MockChatModel(responses=[""])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    try:
        planner({"user_input": "hello", "trace_id": "t"})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "empty response" in str(exc)


def test_llm_planner_wraps_non_json_as_conversational():
    """Creative writing / prose responses are wrapped as conversational, not rejected."""
    essay = "The view outside the window is beautiful."
    llm = MockChatModel(responses=[essay])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    decision = planner({"user_input": "write an essay", "trace_id": "t"})
    assert decision["capability"] == ""
    assert decision["response"] == essay


def test_llm_planner_sync_wraps_malformed_json_as_conversational():
    """Synchronous __call__ still wraps prose for back-compat with the graph path."""
    llm = MockChatModel(responses=['{"capability": "read_file", "arguments":'])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    decision = planner({"user_input": "x", "trace_id": "t"})
    assert decision["capability"] == ""
    assert decision["response"].startswith('{"capability"')


@pytest.mark.asyncio
async def test_llm_planner_astream_plan_falls_back_to_tool_task_on_malformed_json():
    """Broken streaming JSON is routed to tool.task instead of leaking to the UI."""
    broken = (
        '{"capability": "write_file", "arguments": {"path": "x.txt", "content": "line1\n'
        'line2"}}'
    )
    llm = MockChatModel(responses=[broken], chunk_size=4)
    planner = LLMPlanner(llm=llm, available_capabilities=["write_file"])

    text_chunks: list[str] = []
    decision: dict = {}
    async for event in planner.astream_plan({"user_input": "save line1 line2"}):
        if event["type"] == "text":
            text_chunks.append(event["chunk"])
        elif event["type"] == "decision":
            decision = event["decision"]

    assert text_chunks == [], "broken JSON must NOT leak to the UI"
    assert decision["capability"] == "tool.task"
    assert decision["arguments"]["task"] == "save line1 line2"


@pytest.mark.asyncio
async def test_llm_planner_astream_plan_streams_prose():
    essay = "The view outside the window is beautiful."
    llm = MockChatModel(responses=[essay], chunk_size=4)
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])

    text_chunks: list[str] = []
    decision: dict = {}
    async for event in planner.astream_plan({"user_input": "write a short essay"}):
        if event["type"] == "text":
            text_chunks.append(event["chunk"])
        elif event["type"] == "decision":
            decision = event["decision"]

    assert len(text_chunks) >= 2, "prose responses must stream as multiple chunks"
    assert "".join(text_chunks) == essay
    assert decision == {"capability": "", "response": essay}


@pytest.mark.asyncio
async def test_llm_planner_astream_plan_suppresses_json_chunks():
    json_decision = '{"capability": "read_file", "arguments": {"path": "x.md"}}'
    llm = MockChatModel(responses=[json_decision], chunk_size=4)
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])

    text_chunks: list[str] = []
    decision: dict = {}
    async for event in planner.astream_plan({"user_input": "read x.md"}):
        if event["type"] == "text":
            text_chunks.append(event["chunk"])
        elif event["type"] == "decision":
            decision = event["decision"]

    assert text_chunks == [], "JSON tool dispatches must not bleed into the UI"
    assert decision["capability"] == "read_file"
    assert decision["arguments"]["path"] == "x.md"


# ---------------------------------------------------------------------------
# <think> / <thinking> / <reasoning> block stripping.
# ---------------------------------------------------------------------------
# Reasoning models (DeepSeek R1, Qwen-thinking, MiMo, Gemini reasoning)
# emit thought blocks BEFORE the actual response. Without stripping, the
# leading "<" makes the streaming classifier route the whole reply as prose
# and the eventual JSON dispatch silently leaks to the UI instead of being
# dispatched.


def test_strip_think_blocks_removes_closed_pairs():
    raw = "<think>plan: read</think>{\"capability\": \"read_file\"}"
    assert LLMPlanner._strip_think_blocks(raw) == '{"capability": "read_file"}'


def test_strip_think_blocks_handles_multiple_blocks():
    raw = "<think>one</think>middle<thinking>two</thinking>tail"
    assert LLMPlanner._strip_think_blocks(raw) == "middletail"


def test_strip_think_blocks_is_case_insensitive():
    raw = "<THINK>x</THINK>after"
    assert LLMPlanner._strip_think_blocks(raw) == "after"


def test_strip_code_fences_runs_think_strip_first():
    """A common shape from reasoning models: <think>...</think> followed
    by a fenced ```json block. The pipeline must strip the think block AND
    the fence before the planner classifier sees the JSON."""
    raw = "<think>let's pick the read tool</think>\n```json\n{\"capability\": \"read_file\"}\n```"
    cleaned = LLMPlanner._strip_code_fences(raw)
    assert cleaned == '{"capability": "read_file"}'


@pytest.mark.asyncio
async def test_astream_plan_with_think_block_routes_as_json():
    """Regression for the streaming classifier: a leading <think>...</think>
    used to flip mode to 'prose' (because the first non-whitespace token was
    '<'), and the eventual JSON would be rendered as plain text instead of
    dispatched. After the fix, the think block is invisible to the
    classifier and the JSON path wins."""
    response = "<think>user wants the readme</think>{\"capability\": \"read_file\", \"arguments\": {\"path\": \"README.md\"}}"
    llm = MockChatModel(responses=[response], chunk_size=4)
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])

    text_chunks: list[str] = []
    decision: dict = {}
    async for event in planner.astream_plan({"user_input": "read it"}):
        if event["type"] == "text":
            text_chunks.append(event["chunk"])
        elif event["type"] == "decision":
            decision = event["decision"]

    assert text_chunks == [], (
        "think-tag-prefixed JSON must NOT bleed into the UI; got chunks: "
        f"{text_chunks!r}"
    )
    assert decision["capability"] == "read_file"
    assert decision["arguments"]["path"] == "README.md"


@pytest.mark.asyncio
async def test_astream_plan_handles_two_consecutive_think_blocks():
    """The substring-containment state machine missed this case: a model
    emits one think block, opens another mid-prose. With count-based
    matching, the second open is detected and the partial second block
    doesn't leak into the user-visible stream."""
    # Two complete think blocks interleaved with prose. After stripping,
    # only the prose tails ("hello " and " world") should survive.
    response = "<think>first thought</think>hello <thinking>second thought</thinking>world"
    llm = MockChatModel(responses=[response], chunk_size=5)
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])

    text_chunks: list[str] = []
    decision: dict = {}
    async for event in planner.astream_plan({"user_input": "chat"}):
        if event["type"] == "text":
            text_chunks.append(event["chunk"])
        elif event["type"] == "decision":
            decision = event["decision"]

    combined = "".join(text_chunks)
    # The final response is prose, so SOMETHING is streamed. What MUST NOT
    # appear is the raw "thinking" substring — the user should never see the
    # model's scratch work.
    assert "first thought" not in combined, combined
    assert "second thought" not in combined, combined
    # And the final decision is conversational (no capability dispatched).
    assert decision.get("capability") == ""


@pytest.mark.asyncio
async def test_astream_plan_prose_starting_with_stray_angle_bracket():
    """Regression: a prose reply that BEGINS with a '<' that can't grow into a
    <think> tag (e.g. '<3') must classify as prose and stream — not stall in the
    partial-tag guard forever and fall through to the tool.task JSON fallback."""
    response = "<3 I really like this idea, let's go with it."
    llm = MockChatModel(responses=[response], chunk_size=4)
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])

    text_chunks: list[str] = []
    decision: dict = {}
    async for event in planner.astream_plan({"user_input": "what do you think?"}):
        if event["type"] == "text":
            text_chunks.append(event["chunk"])
        elif event["type"] == "decision":
            decision = event["decision"]

    assert decision["capability"] == "", f"stray '<' was misrouted: {decision!r}"
    assert "".join(text_chunks) == response  # streamed as prose, intact


def test_llm_planner_synthesize_returns_natural_response():
    llm = MockChatModel(responses=['The file says: "hello".'])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    result = planner.synthesize(
        user_input="read hello.txt",
        capability="read_file",
        tool_result='{"type":"text","file":{"filePath":"hello.txt","content":"hello\\n","numLines":1}}',
    )
    assert "hello" in result


def test_llm_planner_extracts_text_from_anthropic_content_blocks():
    blocks = [
        {
            "type": "thinking",
            "thinking": "Internal reasoning must not reach the user.",
            "signature": "signed",
            "index": 0,
        },
        {"type": "text", "text": "脚本已保存并运行成功。", "index": 1},
    ]

    class BlockLLM:
        def invoke(self, _messages):
            return SimpleNamespace(content=blocks)

    planner = LLMPlanner(llm=BlockLLM(), available_capabilities=["read_file"])
    decision = planner({"user_input": "你好", "trace_id": "t"})

    assert decision == {
        "capability": "",
        "response": "脚本已保存并运行成功。",
    }


def test_llm_planner_synthesize_extracts_text_from_anthropic_content_blocks():
    blocks = [
        {"type": "thinking", "thinking": "Private chain of thought", "index": 0},
        {"type": "text", "text": "文件内容是 hello。", "index": 1},
    ]

    class BlockLLM:
        def invoke(self, _messages):
            return SimpleNamespace(content=blocks)

    planner = LLMPlanner(llm=BlockLLM(), available_capabilities=["read_file"])

    assert planner.synthesize("读取文件", "read_file", "hello") == "文件内容是 hello。"
