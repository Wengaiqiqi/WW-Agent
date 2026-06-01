from __future__ import annotations

import asyncio
import io

import pytest
from rich.console import Console

from orchestrator.repl_commands import ReplCommandHandler
from orchestrator.repl_controller import REPLController
from orchestrator.repl_state import MultiAgentSessionState
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class _Cfg:
    provider = "openai"
    model = "gpt-4o"
    protocol = "openai"
    base_url = "https://api.openai.com/v1"
    api_key_env = "OPENAI_API_KEY"


class _FakeHost:
    def __init__(self):
        self.calls = []
        self._cancel_called = False

    async def call_tool(self, agent_id, name, arguments):
        self.calls.append((agent_id, name, arguments))
        return {"content": [{"type": "text", "text": "file contents"}]}

    async def cancel_all(self):
        self._cancel_called = True

    def list_handles(self):
        return []


class _FakeRouter:
    def all_capabilities(self):
        return ["read_file"]

    def resolve(self, capability):
        return "tool-agent"

    def describe_tools(self):
        return {}


def _make_controller(tmp_path, **overrides):
    import os
    os.environ["LANGCHAIN_AGENT_MODEL"] = "mock"
    buf = io.StringIO()
    ui = ReplUI(
        console=Console(file=buf, force_terminal=False, width=120),
        input_stream=io.StringIO(), output_stream=buf,
    )
    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[], instruction_files=[],
        memory_snapshot="", workspace=tmp_path,
    )
    host = _FakeHost()
    router = _FakeRouter()
    commands = ReplCommandHandler(ui=ui, state=state, host=host, router=router)
    controller = REPLController(
        host=host if "host" not in overrides else overrides.pop("host"),
        router=router,
        hmac_key="secret",
        state=state,
        commands=commands,
        ui=ui,
        **overrides,
    )
    return controller, ui, state, host, router, buf


@pytest.mark.asyncio
async def test_handle_input_routes_slash_commands(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    result = await controller.handle_input("/help")
    assert result == LoopAction.CONTINUE
    assert "Slash Commands" in buf.getvalue()


@pytest.mark.asyncio
async def test_handle_input_executes_normal_turn(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    result = await controller.handle_input("read_file:README.md")
    assert result == LoopAction.CONTINUE
    assert state.turns == 1
    assert host.calls


@pytest.mark.asyncio
async def test_streaming_conversational_response_renders_text(tmp_path):
    """Prose answers stream into the TUI and land in session history."""
    from agents.shared.mock_chat_model import MockChatModel
    from orchestrator.turns import LLMPlanner

    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    essay = "The spring breeze crossed the desk."
    llm = MockChatModel(responses=[essay], chunk_size=4)
    controller._planner = LLMPlanner(
        llm=llm, available_capabilities=router.all_capabilities(),
    )

    result = await controller._execute_turn("please share a tiny vignette")
    assert result == LoopAction.CONTINUE
    assert state.turns == 1
    out = buf.getvalue()
    assert "spring breeze" in out
    assert "[multi-agent]:" in out
    # Conversational path must not call any tool.
    assert host.calls == []


def test_tool_line_active_pulses_without_relying_on_ansi_blink():
    """The running bullet must self-animate, not lean on the `blink` SGR that
    Windows Terminal / VS Code drop. ``active=True`` returns a time-driven
    renderable whose bullet style changes between refreshes."""
    from rich.console import Console
    from orchestrator.repl_controller import (
        _build_tool_line, _PulsingToolLine, _tool_line_text,
    )

    active = _build_tool_line("web_extract", {"url": "https://x"}, active=True)
    assert isinstance(active, _PulsingToolLine)

    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    # Render at two clock phases ~0.5s apart; the bullet style must differ so
    # the dot visibly throbs even where ANSI blink is ignored.
    import orchestrator.repl_controller as rc
    seen = set()
    real_monotonic = rc.time.monotonic
    try:
        for t in (0.0, 0.5):
            rc.time.monotonic = lambda _t=t: _t
            with console.capture() as cap:
                console.print(active)
            seen.add(cap.get())
    finally:
        rc.time.monotonic = real_monotonic
    assert len(seen) == 2, "pulsing bullet did not change between refreshes"

    # Frozen (result arrived) is a plain static line, not the pulsing wrapper.
    frozen = _build_tool_line("web_extract", {"url": "https://x"}, active=False)
    assert not isinstance(frozen, _PulsingToolLine)
    assert "web_extract" in frozen.plain


def test_fast_route_obvious_project_work_skips_planner(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    plan = controller._fast_route("review整个项目 and optimize Loading")
    assert plan == {
        "capability": "tool.task",
        "arguments": {"task": "review整个项目 and optimize Loading"},
    }


def test_fast_route_leaves_plain_chat_for_planner(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    assert controller._fast_route("hello there") is None


@pytest.mark.asyncio
async def test_mcp_capability_turn_invokes_planner_only_once(tmp_path):
    """Regression: the REPL planned a turn, then handed the LIVE planner to
    TurnRunner for an MCP capability — which re-ran the planner, a second full
    LLM round-trip. The decision must be pinned so the planner runs exactly
    once."""
    controller, ui, state, host, router, buf = _make_controller(tmp_path)

    class _CountingPlanner:
        def __init__(self, decision):
            self._decision = decision
            self.calls = 0

        async def astream_plan(self, _plan_input):
            self.calls += 1
            yield {"type": "decision", "decision": dict(self._decision)}

        def __call__(self, _state):
            # Pre-fix, TurnRunner re-invoked the planner here (a second call).
            self.calls += 1
            return dict(self._decision)

    planner = _CountingPlanner({"capability": "read_file",
                                "arguments": {"path": "README.md"}})
    controller._planner = planner

    result = await controller._execute_turn("please open the readme")

    assert result == LoopAction.CONTINUE
    assert planner.calls == 1, "planner must be consulted exactly once per turn"
    # The MCP tool actually ran with the pinned decision.
    assert host.calls and host.calls[0][1] == "read_file"


@pytest.mark.asyncio
async def test_execute_turn_catches_exception_as_recoverable(tmp_path):
    class _ExplodingHost:
        async def call_tool(self, agent_id, name, arguments):
            raise ConnectionError("specialist unreachable")
        async def cancel_all(self):
            pass
        def list_handles(self):
            return []

    controller, ui, state, host, router, buf = _make_controller(
        tmp_path, host=_ExplodingHost(),
    )
    result = await controller._execute_turn("read_file:foo")
    assert result == LoopAction.CONTINUE
    assert "specialist unreachable" in buf.getvalue()
    assert state.turns == 1  # recorded even on error


@pytest.mark.asyncio
async def test_is_fatal_returns_true_for_cancelled_error(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    assert controller._is_fatal(asyncio.CancelledError()) is True


@pytest.mark.asyncio
async def test_is_fatal_returns_false_for_common_errors(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    assert controller._is_fatal(ConnectionError("x")) is False
    assert controller._is_fatal(ValueError("x")) is False
    assert controller._is_fatal(RuntimeError("x")) is False


@pytest.mark.asyncio
async def test_planner_starts_none(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    assert controller._planner is None


@pytest.mark.asyncio
async def test_cancelled_error_during_turn_calls_cancel_all(tmp_path):
    class _CancellingHost:
        def __init__(self):
            self._cancel_called = False

        async def call_tool(self, agent_id, name, arguments):
            raise asyncio.CancelledError()

        async def cancel_all(self):
            self._cancel_called = True

        def list_handles(self):
            return []

    host = _CancellingHost()
    controller, ui, state, _host, router, buf = _make_controller(tmp_path, host=host)
    result = await controller._execute_turn("read_file:foo")
    assert result == LoopAction.CONTINUE
    assert host._cancel_called
    assert "Cancelled" in buf.getvalue()
