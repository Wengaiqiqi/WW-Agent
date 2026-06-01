"""Tests for /comm, /task, /chat slash commands."""
from __future__ import annotations

import asyncio
import io
import json
import os

from rich.console import Console

from orchestrator.registry import Card
from orchestrator.repl_commands import ReplCommandHandler, _unwrap
from orchestrator.repl_state import MultiAgentSessionState
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


def _call(handler: ReplCommandHandler, line: str):
    return asyncio.run(handler.handle(line))


class _Cfg:
    provider = "mock"
    model = "mock-model"
    protocol = "openai"
    base_url = "http://mock.invalid/v1"
    api_key_env = "MOCK_API_KEY"


class _Handle:
    def __init__(self):
        self.card = Card(
            id="comm-agent", display_name="Comm", version="1.0.0",
            entrypoint={}, mcp={}, a2a={},
            capabilities_hint=["comm"], model_override=None,
        )
        self.a2a_url = None


class _MockHost:
    """Host that records call_tool invocations and returns preset results."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self._responses: dict[str, str] = {}

    def list_handles(self):
        return [_Handle()]

    def set_response(self, tool_name: str, response_json: dict) -> None:
        self._responses[tool_name] = json.dumps(response_json)

    async def call_tool(self, agent_id: str, name: str, arguments: dict):
        self.calls.append((agent_id, name, arguments))
        text = self._responses.get(name, '{"ok": true}')
        # Return MCP-like object with content attribute
        return _FakeResult(text=text, is_error=False)


class _FakeResult:
    """Mimics MCP SDK CallToolResult."""

    def __init__(self, text: str, is_error: bool = False):
        self.isError = is_error
        self.content = [_FakeContent(text)]


class _FakeContent:
    def __init__(self, text: str):
        self.text = text


class _Router:
    def all_capabilities(self):
        return []

    def resolve(self, capability):
        return "comm-agent"


def _make(tmp_path):
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
    host = _MockHost()
    handler = ReplCommandHandler(ui=ui, state=state, host=host, router=_Router())
    return handler, ui, state, buf, host


# ---- _unwrap tests ----


def test_unwrap_object_success():
    result = _FakeResult(text='{"ok":true}', is_error=False)
    is_err, text = _unwrap(result)
    assert not is_err
    assert text == '{"ok":true}'


def test_unwrap_object_error():
    result = _FakeResult(text="something failed", is_error=True)
    is_err, text = _unwrap(result)
    assert is_err
    assert text == "something failed"


def test_unwrap_dict_error():
    result = {
        "content": [{"type": "text", "text": "specialist crashed"}],
        "isError": True,
    }
    is_err, text = _unwrap(result)
    assert is_err
    assert "crashed" in text


def test_unwrap_dict_success():
    result = {
        "content": [{"type": "text", "text": '{"ok":true}'}],
        "isError": False,
    }
    is_err, text = _unwrap(result)
    assert not is_err
    assert text == '{"ok":true}'


# ---- /comm list ----


def test_comm_list_renders_table(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    host.set_response("comm.list_peers", {
        "peers": [
            {"peer_id": "remote-1", "display_name": "Remote One", "url": "https://r1.example.com", "last_seen": None},
            {"peer_id": "remote-2", "display_name": "Remote Two", "url": "https://r2.example.com", "last_seen": None},
        ]
    })
    handler._current_peer = "remote-1"
    result = _call(handler, "/comm list")
    assert result == LoopAction.CONTINUE
    text = buf.getvalue()
    assert "remote-1" in text
    assert "remote-2" in text
    assert "★" in text


def test_comm_list_no_star_when_no_current(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    host.set_response("comm.list_peers", {
        "peers": [{"peer_id": "p1", "display_name": "P", "url": "http://x", "last_seen": None}]
    })
    _call(handler, "/comm list")
    # No star marker since no _current_peer
    text = buf.getvalue()
    assert "p1" in text


# ---- /comm use ----


def test_comm_use_switches_current(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    host.set_response("comm.list_peers", {
        "peers": [{"peer_id": "alpha", "display_name": "", "url": "", "last_seen": None}]
    })
    result = _call(handler, "/comm use alpha")
    assert result == LoopAction.CONTINUE
    assert handler._current_peer == "alpha"
    assert "Switched" in buf.getvalue()


def test_comm_use_unknown_peer_errors(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    host.set_response("comm.list_peers", {"peers": []})
    result = _call(handler, "/comm use ghost")
    assert result == LoopAction.CONTINUE
    assert handler._current_peer is None
    assert "not found" in buf.getvalue()


# ---- /comm rm ----


def test_comm_rm_clears_current_peer(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    handler._current_peer = "target"
    handler._chat_contexts["target"] = "ctx-123"
    host.set_response("comm.remove_peer", {"ok": True, "peer_id": "target", "removed": True})
    result = _call(handler, "/comm rm target")
    assert result == LoopAction.CONTINUE
    assert handler._current_peer is None
    assert "target" not in handler._chat_contexts
    assert "removed" in buf.getvalue().lower()


def test_comm_rm_other_peer_keeps_current(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    handler._current_peer = "keep-me"
    host.set_response("comm.remove_peer", {"ok": True, "peer_id": "other", "removed": True})
    _call(handler, "/comm rm other")
    assert handler._current_peer == "keep-me"


# ---- /comm (no subcommand) ----


def test_comm_no_sub_shows_usage(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    result = _call(handler, "/comm")
    assert result == LoopAction.CONTINUE
    assert "Usage" in buf.getvalue()


# ---- /task ----


def test_task_no_current_peer_errors(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    result = _call(handler, "/task do something")
    assert result == LoopAction.CONTINUE
    assert "No current peer" in buf.getvalue()


def test_task_delegates_with_correct_args(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    handler._current_peer = "worker"
    host.set_response("comm.list_peers", {
        "peers": [{"peer_id": "worker", "display_name": "", "url": "https://w.test", "last_seen": None}]
    })
    host.set_response("comm.delegate", {
        "ok": True, "events_count": 3, "final_result": {"parts": [{"text": "done!"}]}, "duration_ms": 150,
    })
    result = _call(handler, "/task build the thing")
    assert result == LoopAction.CONTINUE
    # Verify call was made with stream=False
    delegate_call = [c for c in host.calls if c[1] == "comm.delegate"]
    assert len(delegate_call) == 1
    assert delegate_call[0][2]["stream"] is False
    assert delegate_call[0][2]["task"] == "build the thing"
    text = buf.getvalue()
    assert "done!" in text
    assert "worker" in text


def test_task_empty_message_shows_usage(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    handler._current_peer = "worker"
    result = _call(handler, "/task")
    assert result == LoopAction.CONTINUE
    assert "Usage" in buf.getvalue()


# ---- /chat ----


def test_chat_no_current_peer_errors(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    result = _call(handler, "/chat hello")
    assert result == LoopAction.CONTINUE
    assert "No current peer" in buf.getvalue()


def test_chat_first_turn_no_context(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    handler._current_peer = "bot"
    host.set_response("comm.list_peers", {
        "peers": [{"peer_id": "bot", "display_name": "", "url": "https://b.test", "last_seen": None}]
    })
    host.set_response("comm.chat", {
        "ok": True, "reply": "hi there!", "context_id": "ctx-new",
    })
    result = _call(handler, "/chat hello")
    assert result == LoopAction.CONTINUE
    # context_id=None on first call
    chat_call = [c for c in host.calls if c[1] == "comm.chat"]
    assert chat_call[0][2]["context_id"] is None
    # context stored
    assert handler._chat_contexts["bot"] == "ctx-new"
    assert "hi there!" in buf.getvalue()


def test_chat_subsequent_carries_context(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    handler._current_peer = "bot"
    handler._chat_contexts["bot"] = "ctx-existing"
    host.set_response("comm.list_peers", {
        "peers": [{"peer_id": "bot", "display_name": "", "url": "https://b.test", "last_seen": None}]
    })
    host.set_response("comm.chat", {
        "ok": True, "reply": "continued", "context_id": "ctx-existing",
    })
    _call(handler, "/chat how are you")
    chat_call = [c for c in host.calls if c[1] == "comm.chat"]
    assert chat_call[0][2]["context_id"] == "ctx-existing"


def test_chat_contexts_independent_per_peer(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    handler._current_peer = "peer-a"
    handler._chat_contexts["peer-a"] = "ctx-a"
    handler._chat_contexts["peer-b"] = "ctx-b"
    host.set_response("comm.list_peers", {
        "peers": [
            {"peer_id": "peer-a", "display_name": "", "url": "", "last_seen": None},
            {"peer_id": "peer-b", "display_name": "", "url": "", "last_seen": None},
        ]
    })
    host.set_response("comm.chat", {"ok": True, "reply": "ok", "context_id": "ctx-a"})
    _call(handler, "/chat msg1")
    chat_call = [c for c in host.calls if c[1] == "comm.chat"]
    assert chat_call[0][2]["context_id"] == "ctx-a"


# ---- /comm add (execute layer) ----


def test_comm_add_execute_sets_current(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    host.set_response("comm.add_peer", {
        "ok": True, "peer_id": "new-peer", "env_var_name": "COMM_PEER_NEW_PEER_HMAC",
        "fetched_card": None,
        "note": "persist env var: export COMM_PEER_NEW_PEER_HMAC=<value>",
    })
    result = asyncio.run(handler._comm_add_execute(
        peer_id="new-peer", url="https://n.test",
        display_name="New Peer", hmac_secret="s3cr3t",
    ))
    assert result == LoopAction.CONTINUE
    assert handler._current_peer == "new-peer"
    text = buf.getvalue()
    assert "new-peer" in text
    assert "COMM_PEER_NEW_PEER_HMAC" in text


def test_comm_add_execute_with_pinned_sha256(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)
    host.set_response("comm.add_peer", {
        "ok": True, "peer_id": "pinned", "env_var_name": "COMM_PEER_PINNED_HMAC",
        "fetched_card": None, "note": "persist env var: ...",
    })
    asyncio.run(handler._comm_add_execute(
        peer_id="pinned", url="https://p.test",
        display_name="Pinned", hmac_secret="key",
        tls_verify=False, tls_pinned_sha256="abcdef123456",
    ))
    add_call = [c for c in host.calls if c[1] == "comm.add_peer"]
    assert add_call[0][2]["tls_verify"] is False
    assert add_call[0][2]["tls_pinned_sha256"] == "abcdef123456"


# ---- comm-agent unavailable ----


def test_comm_call_error_renders_friendly(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)

    # Override host to return error
    async def _error_call(agent_id, name, arguments):
        return {"content": [{"type": "text", "text": "error: specialist 'comm-agent' unavailable"}], "isError": True}
    host.call_tool = _error_call

    result = _call(handler, "/comm list")
    assert result == LoopAction.CONTINUE
    assert "comm-agent error" in buf.getvalue()


def test_comm_call_invalid_json_renders_friendly(tmp_path):
    handler, ui, state, buf, host = _make(tmp_path)

    async def _bad_json(agent_id, name, arguments):
        return _FakeResult(text="not json at all", is_error=False)
    host.call_tool = _bad_json

    result = _call(handler, "/comm list")
    assert result == LoopAction.CONTINUE
    assert "invalid response" in buf.getvalue()
