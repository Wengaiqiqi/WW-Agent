import os
import time
import jwt as pyjwt
import pytest
from agents.tool_agent.tool_executor import (
    build_tool_specs,
    execute_tool,
    make_langchain_tools,
)


TEST_KEY = "test-tool-executor-key"


@pytest.fixture(autouse=True)
def _set_hmac_key(monkeypatch):
    monkeypatch.setenv("AUTHZ_HMAC_KEY", TEST_KEY)


def _grant(tool: str) -> str:
    return pyjwt.encode(
        {
            "iss": "orchestrator", "sub": "tool-agent",
            "exp": int(time.time()) + 60,
            "permission_mode": "workspace-write",
            "allowed_tools": [tool], "trace_id": "t1",
        },
        TEST_KEY, algorithm="HS256",
    )


def test_tool_specs_include_read_file():
    specs = build_tool_specs()
    names = {s.name for s in specs}
    assert "read_file" in names


@pytest.mark.asyncio
async def test_execute_read_file(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hi there", encoding="utf-8")
    result = await execute_tool("read_file", {
        "path": str(target),
        "_meta": {"authz_grant": _grant("read_file")},
    })
    assert "hi there" in str(result)


@pytest.mark.asyncio
async def test_execute_unknown_tool_raises():
    with pytest.raises(ValueError, match="unknown tool"):
        await execute_tool("not_a_tool", {})  # unknown tool fails before authz


def test_run_python_and_run_command_hidden_from_mcp_specs():
    """Shell tools must NOT be MCP-registered — the orchestrator's planner
    must not be able to dispatch them directly. They live behind tool.task."""
    names = {s.name for s in build_tool_specs()}
    assert "run_python" not in names
    assert "run_command" not in names


def test_clarify_hidden_from_mcp_specs():
    """``clarify`` must not be MCP-registered. The synchronous MCP path has
    no UI callback channel, so a direct planner dispatch would just hang.
    The tool stays available to tool-agent's ReAct loop via the streaming
    A2A path + clarify_bridge."""
    names = {s.name for s in build_tool_specs()}
    assert "clarify" not in names


def test_clarify_available_in_react_tools():
    """ReAct loop needs clarify available so the model can choose to ask
    the user mid-turn. Pair test to the MCP-hidden check above."""
    names = {t.name for t in make_langchain_tools()}
    assert "clarify" in names


@pytest.mark.asyncio
async def test_clarify_wrapper_uses_bridge(monkeypatch):
    """The clarify wrapper must route through ``clarify_bridge.request`` so the
    SSE → user → SSE round-trip works. We patch the bridge and assert the
    wrapper called it and returned the bridge's answer."""
    import json
    from agents.tool_agent import clarify_bridge, tool_executor

    async def _fake_request(question, choices):
        assert question == "color?"
        assert choices == ["red", "blue"]
        return "red"

    monkeypatch.setattr(clarify_bridge, "request", _fake_request)
    result = await tool_executor._wrap_clarify({
        "question": "color?",
        "choices": ["red", "blue"],
    })
    parsed = json.loads(result)
    assert parsed["user_response"] == "red"


@pytest.mark.asyncio
async def test_clarify_wrapper_rejects_empty_question():
    import json
    from agents.tool_agent import tool_executor

    result = await tool_executor._wrap_clarify({"question": "   "})
    parsed = json.loads(result)
    assert "error" in parsed


def test_run_python_and_run_command_available_to_react_loop():
    """tool-agent's internal ReAct loop must still see them as LangChain tools."""
    names = {t.name for t in make_langchain_tools()}
    assert "run_python" in names
    assert "run_command" in names


@pytest.mark.asyncio
async def test_execute_run_python_emits_stdout():
    result = await execute_tool("run_python", {
        "code": "print(2 + 3)",
        "_meta": {"authz_grant": _grant("run_python")},
    })
    assert "5" in str(result)


@pytest.mark.asyncio
async def test_execute_run_command_emits_stdout():
    # `echo` is portable across cmd.exe and /bin/sh.
    result = await execute_tool("run_command", {
        "command": "echo hello-shell",
        "_meta": {"authz_grant": _grant("run_command")},
    })
    assert "hello-shell" in str(result)
