# tests/test_orchestrator/test_llm_planner.py
import pytest

from orchestrator.turns import LLMPlanner
from agents.shared.mock_chat_model import MockChatModel


def test_llm_planner_emits_structured_decision():
    llm = MockChatModel(responses=[
        '{"capability": "read_file", "arguments": {"path": "README.md"}}'
    ])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file", "skill.ppt-master"])
    decision = planner({"user_input": "read the readme", "trace_id": "t"})
    assert decision["capability"] == "read_file"
    assert decision["arguments"]["path"] == "README.md"


def test_llm_planner_strips_code_fences():
    llm = MockChatModel(responses=[
        '```json\n{"capability": "read_file", "arguments": {"path": "x"}}\n```'
    ])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    decision = planner({"user_input": "read x", "trace_id": "t"})
    assert decision["capability"] == "read_file"


def test_llm_planner_returns_conversational_response():
    llm = MockChatModel(responses=[
        '{"capability": "", "response": "你好！我是智能助手，有什么可以帮你的？"}'
    ])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    decision = planner({"user_input": "你好", "trace_id": "t"})
    assert decision["capability"] == ""
    assert "你好" in decision["response"]


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
    essay = "窗外的风景真美。春去秋来，我总爱在窗边看风景。"
    llm = MockChatModel(responses=[essay])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    decision = planner({"user_input": "写一个作文", "trace_id": "t"})
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
    """When the planner emits broken JSON during streaming, never dump the raw
    bytes to the user. Hand the original request to tool-agent instead."""
    # Long content with a literal newline inside a JSON string — invalid JSON.
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
    essay = "窗外有一只小鸟在唱歌，阳光透过玻璃洒在书桌上。"
    llm = MockChatModel(responses=[essay], chunk_size=4)
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])

    text_chunks: list[str] = []
    decision: dict = {}
    async for event in planner.astream_plan({"user_input": "写一篇短文"}):
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


def test_llm_planner_synthesize_returns_natural_response():
    llm = MockChatModel(responses=[
        "文件内容为：\"你在干什么\"，只有1行。用户问的是查看文件，已成功读取。"
    ])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    result = planner.synthesize(
        user_input="查看你好.txt",
        capability="read_file",
        tool_result='{"type":"text","file":{"filePath":"你好.txt","content":"你在干什么\n","numLines":1}}',
    )
    assert "你在干什么" in result
