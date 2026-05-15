from __future__ import annotations

import io
import os
from pathlib import Path

from rich.console import Console

from orchestrator.registry import Card
from orchestrator.repl_commands import ReplCommandHandler
from orchestrator.repl_state import MultiAgentSessionState
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class _Cfg:
    provider = "mock"
    model = "mock-model"
    protocol = "openai"
    base_url = "http://mock.invalid/v1"
    api_key_env = "MOCK_API_KEY"


class _Handle:
    def __init__(self):
        self.card = Card(
            id="tool-agent", display_name="Tool", version="1.0.0",
            entrypoint={}, mcp={}, a2a={},
            capabilities_hint=["read_file"], model_override=None,
        )
        self.a2a_url = "http://127.0.0.1:50001"


class _Host:
    def list_handles(self):
        return [_Handle()]


class _Router:
    def all_capabilities(self):
        return ["read_file", "write_file"]

    def resolve(self, capability):
        return "tool-agent"


def _handler(tmp_path):
    os.environ.pop("LANGCHAIN_AGENT_PERMISSION_MODE", None)
    buf = io.StringIO()
    ui = ReplUI(
        console=Console(file=buf, force_terminal=False, width=120),
        input_stream=io.StringIO(), output_stream=buf,
    )
    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[], instruction_files=[],
        memory_snapshot="memory", workspace=tmp_path,
    )
    return ReplCommandHandler(ui=ui, state=state, host=_Host(), router=_Router()), ui, state, buf


def test_help_continues_and_renders(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    result = handler.handle("/help")
    assert result == LoopAction.CONTINUE
    assert "Slash Commands" in buf.getvalue()


def test_exit_and_quit_return_exit(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/exit") == LoopAction.EXIT
    assert handler.handle("/quit") == LoopAction.EXIT


def test_agents_renders_table(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/agents") == LoopAction.CONTINUE
    assert "tool-agent" in buf.getvalue()


def test_tools_renders_capabilities(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/tools") == LoopAction.CONTINUE
    text = buf.getvalue()
    assert "read_file" in text
    assert "write_file" in text


def test_permissions_shows_current(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/permissions") == LoopAction.CONTINUE
    assert "workspace-write" in buf.getvalue()


def test_permissions_updates_state(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/permissions read-only") == LoopAction.CONTINUE
    assert state.permission_mode == "read-only"
    assert "read-only" in buf.getvalue()


def test_permissions_invalid_mode(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/permissions bogus") == LoopAction.CONTINUE
    assert "Invalid" in buf.getvalue()
    assert state.permission_mode == "workspace-write"


def test_config_renders_table(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/config") == LoopAction.CONTINUE
    text = buf.getvalue()
    assert "mock" in text
    assert "mock-model" in text


def test_clear_returns_continue(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/clear") == LoopAction.CONTINUE


def test_compact_resets_history(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    state.record_turn(
        user_input="x", capability="read_file",
        owner="tool-agent", observation="y", error=None,
    )
    assert handler.handle("/compact") == LoopAction.CONTINUE
    assert state.recent_history == []
    assert state.thread_id == "multi-agent-session-2"
    assert "Compacted" in buf.getvalue()


def test_unknown_command_warns(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/nope") == LoopAction.CONTINUE
    assert "Unknown command" in buf.getvalue()


def test_non_slash_input_returns_none(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    result = handler.handle("hello world")
    assert result is None
