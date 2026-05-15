import os
import time
import jwt as pyjwt
import pytest
from agents.tool_agent.tool_executor import build_tool_specs, execute_tool


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
