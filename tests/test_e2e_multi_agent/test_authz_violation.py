import os
import sys
import time
import pytest
import jwt as pyjwt
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


HMAC_KEY = "test-key-for-authz"


def _grant(allowed: list[str], expired: bool = False, key: str = HMAC_KEY) -> str:
    return pyjwt.encode(
        {
            "iss": "orchestrator", "sub": "tool-agent",
            "exp": int(time.time()) + (-1 if expired else 60),
            "permission_mode": "workspace-write",
            "allowed_tools": allowed, "trace_id": "t1",
        },
        key, algorithm="HS256",
    )


def _error_text(result) -> str:
    """Extract the error message text from an MCP tool call result."""
    if result.content:
        return " ".join(c.text for c in result.content if hasattr(c, "text"))
    return ""


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_off_whitelist_tool_rejected():
    env = os.environ.copy()
    env["AUTHZ_HMAC_KEY"] = HMAC_KEY
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agents.tool_agent.main"], env=env,
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(
                "read_file",
                arguments={
                    "path": "README.md",
                    "_meta": {"authz_grant": _grant(["write_file"])},
                },
            )
            assert result.isError, "Expected isError=True for off-whitelist tool"
            msg = _error_text(result)
            assert "allowed_tools" in msg or "authz" in msg.lower(), (
                f"Expected authz/allowed_tools in error, got: {msg!r}"
            )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_expired_grant_rejected():
    env = os.environ.copy()
    env["AUTHZ_HMAC_KEY"] = HMAC_KEY
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agents.tool_agent.main"], env=env,
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(
                "read_file",
                arguments={
                    "path": "README.md",
                    "_meta": {"authz_grant": _grant(["read_file"], expired=True)},
                },
            )
            assert result.isError, "Expected isError=True for expired grant"
            msg = _error_text(result)
            assert "expired" in msg.lower(), (
                f"Expected 'expired' in error, got: {msg!r}"
            )
