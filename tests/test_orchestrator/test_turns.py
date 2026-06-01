from __future__ import annotations

from io import StringIO

import pytest

from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux
from orchestrator.turns import LLMPlanner, TurnRunner, _stub_planner, run_prompt_once


class _Text:
    def __init__(self, text: str):
        self.text = text


class _FakeHost:
    def __init__(self):
        self.calls = []

    async def call_tool(self, agent_id: str, name: str, arguments: dict):
        self.calls.append((agent_id, name, arguments))
        return {"content": [{"type": "text", "text": "file contents"}]}


class _FakeLLMResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return _FakeLLMResponse(self.content)


def test_stub_planner_supports_capability_colon_arg():
    decision = _stub_planner({"user_input": "read_file:README.md"})
    assert decision == {"capability": "read_file", "arguments": {"path": "README.md"}}


def test_llm_planner_includes_session_context():
    llm = _FakeLLM('{"capability": "read_file", "arguments": {"path": "README.md"}}')
    planner = LLMPlanner(
        llm=llm,
        available_capabilities=["read_file"],
        context_provider=lambda: "Recent history: user asked about README",
    )

    decision = planner({"user_input": "read it", "trace_id": "t1"})

    assert decision["capability"] == "read_file"
    assert "Recent history" in llm.messages[1]["content"]


@pytest.mark.asyncio
async def test_turn_runner_dispatches_and_normalizes_text():
    router = CapabilityRouter()
    router.register("tool-agent", ["read_file"])
    host = _FakeHost()

    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key="secret",
        permission_mode_provider=lambda: "workspace-write",
        planner=lambda state: {"capability": "read_file", "arguments": {"path": "README.md"}},
    )

    result = await runner.run("read README", trace_id="t1")

    assert result.error is None
    assert result.capability == "read_file"
    assert result.owner == "tool-agent"
    assert result.text == "file contents"
    assert host.calls[0][0] == "tool-agent"
    assert host.calls[0][1] == "read_file"
    assert host.calls[0][2]["path"] == "README.md"
    assert "authz_grant" in host.calls[0][2]["_meta"]


@pytest.mark.asyncio
async def test_turn_runner_returns_error_for_planner_exception():
    router = CapabilityRouter()
    router.register("tool-agent", ["read_file"])
    host = _FakeHost()

    def bad_planner(state):
        raise ValueError("planner exploded")

    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key="secret",
        permission_mode_provider=lambda: "workspace-write",
        planner=bad_planner,
    )

    result = await runner.run("read README", trace_id="t1")

    assert result.error == "planner exploded"
    assert host.calls == []


@pytest.mark.asyncio
async def test_turn_runner_returns_conversational_response():
    router = CapabilityRouter()
    router.register("tool-agent", ["read_file"])
    host = _FakeHost()

    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key="secret",
        permission_mode_provider=lambda: "workspace-write",
        planner=lambda state: {"capability": "", "response": "你好！有什么可以帮你的？"},
    )

    result = await runner.run("你好", trace_id="t1")

    assert result.error is None
    assert result.capability == ""
    assert result.owner == "orchestrator"
    assert "你好" in result.text
    assert host.calls == []


@pytest.mark.asyncio
async def test_turn_runner_synthesizes_tool_result():
    router = CapabilityRouter()
    router.register("tool-agent", ["read_file"])
    host = _FakeHost()

    class _SynthesizingPlanner:
        def __call__(self, state):
            return {"capability": "read_file", "arguments": {"path": "x"}}

        def synthesize(self, user_input, capability, tool_result):
            return f"Successfully read the file. Content: 你好世界"

    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key="secret",
        permission_mode_provider=lambda: "workspace-write",
        planner=_SynthesizingPlanner(),
    )

    result = await runner.run("read x", trace_id="t1")

    assert result.error is None
    assert result.owner == "orchestrator"
    assert "你好世界" in result.text
    assert host.calls == [("tool-agent", "read_file", {"path": "x", "_meta": host.calls[0][2]["_meta"]})]


@pytest.mark.asyncio
async def test_turn_runner_delegates_tool_task_via_a2a():
    """tool.task must stream through A2A delegation, NOT the MCP host path.

    Regression guard for the one-shot ``cli.py prompt`` bug where tool.task
    hit tool-agent's MCP server (which has no such tool) and failed with
    ``unknown tool: tool.task``.
    """
    router = CapabilityRouter()
    router.register("tool-agent", ["tool.task"])
    host = _FakeHost()  # must NOT be touched for tool.task

    async def fake_delegate(*, peer_id, task, meta, context=""):
        assert peer_id == "tool-agent"
        assert task == "say hi"
        assert "authz_grant" in meta
        yield {"type": "text", "chunk": "hello "}
        yield {"type": "text", "chunk": "world"}
        yield {"type": "done", "text": "hello world"}

    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key="secret",
        permission_mode_provider=lambda: "workspace-write",
        planner=lambda state: {"capability": "tool.task", "arguments": {"task": "say hi"}},
        delegate=fake_delegate,
    )

    result = await runner.run("say hi", trace_id="t1")

    assert result.error is None
    assert result.capability == "tool.task"
    assert result.owner == "tool-agent"
    assert result.text == "hello world"
    assert host.calls == []


@pytest.mark.asyncio
async def test_turn_runner_delegates_skill_via_a2a():
    router = CapabilityRouter()
    router.register("skill-agent", ["skill.demo"])
    host = _FakeHost()

    captured: dict = {}

    async def fake_delegate(*, peer_id, task, meta, context=""):
        captured["peer_id"] = peer_id
        captured["meta"] = meta
        yield {"type": "done", "text": "skill done"}

    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key="secret",
        permission_mode_provider=lambda: "workspace-write",
        planner=lambda state: {"capability": "skill.demo", "arguments": {"q": "x"}},
        delegate=fake_delegate,
    )

    result = await runner.run("do demo", trace_id="t1")

    assert result.error is None
    assert result.capability == "skill.demo"
    assert result.text == "skill done"
    assert captured["peer_id"] == "skill-agent"
    assert captured["meta"].get("skill_slug") == "demo"
    assert host.calls == []


@pytest.mark.asyncio
async def test_turn_runner_fast_routes_obvious_tool_task_without_planner_llm():
    """An obvious file/command request skips the planner LLM round-trip.

    With a real ``LLMPlanner``, the one-shot path should recognize
    "read README.md" as tool-agent work and delegate over A2A directly —
    never calling ``llm.invoke``.
    """
    router = CapabilityRouter()
    router.register("tool-agent", ["tool.task"])
    host = _FakeHost()
    llm = _FakeLLM('{"capability": "", "response": "should not be used"}')
    planner = LLMPlanner(llm=llm, available_capabilities=router.all_capabilities())

    captured: dict = {}

    async def fake_delegate(*, peer_id, task, meta, context=""):
        captured["task"] = task
        yield {"type": "done", "text": "done via fast route"}

    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key="secret",
        permission_mode_provider=lambda: "workspace-write",
        planner=planner,
        delegate=fake_delegate,
    )

    result = await runner.run("read README.md", trace_id="t1")

    assert result.error is None
    assert result.capability == "tool.task"
    assert result.text == "done via fast route"
    assert captured["task"] == "read README.md"
    # The planner LLM must NOT have been consulted.
    assert llm.messages is None
    assert host.calls == []


@pytest.mark.asyncio
async def test_turn_runner_falls_through_to_planner_for_chat():
    """Plain chat does not fast-route — the LLM planner still runs."""
    router = CapabilityRouter()
    router.register("tool-agent", ["tool.task"])
    host = _FakeHost()
    llm = _FakeLLM('{"capability": "", "response": "你好！"}')
    planner = LLMPlanner(llm=llm, available_capabilities=router.all_capabilities())

    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key="secret",
        permission_mode_provider=lambda: "workspace-write",
        planner=planner,
    )

    result = await runner.run("hello there", trace_id="t1")

    assert result.error is None
    assert result.capability == ""
    assert "你好" in result.text
    # Plain chat fell through to the planner — the LLM was consulted.
    assert llm.messages is not None


@pytest.mark.asyncio
async def test_run_prompt_once_emits_orchestrator_error_for_turn_error():
    router = CapabilityRouter()
    router.register("tool-agent", ["read_file"])
    host = _FakeHost()
    out = StringIO()

    def bad_planner(state):
        raise ValueError("planner exploded")

    code = await run_prompt_once(
        prompt="read README",
        host=host,
        router=router,
        hmac_key="secret",
        planner=bad_planner,
        permission_mode_provider=lambda: "workspace-write",
        mux=StreamMux(out),
    )

    assert code == 1
    assert "[orchestrator] error: planner exploded" in out.getvalue()
