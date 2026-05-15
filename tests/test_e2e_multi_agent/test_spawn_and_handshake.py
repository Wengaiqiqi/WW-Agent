import asyncio
import os
import sys
import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_tool_agent_spawn_and_list_tools():
    """Orchestrator-style: spawn tool-agent via subprocess + MCP stdio client,
    initialize the session, list its tools."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agents.tool_agent.main"],
        env=os.environ.copy(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "read_file" in names
