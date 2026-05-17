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
