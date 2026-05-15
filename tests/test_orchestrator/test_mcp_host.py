import os
import pytest
from orchestrator.registry import Card
from orchestrator.mcp_host import MCPHost


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_mcp_host_spawns_tool_agent_and_calls_read_file(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hi there", encoding="utf-8")

    card = Card(
        id="tool-agent", display_name="T", version="1",
        entrypoint={"type": "python", "module": "agents.tool_agent.main", "args": []},
        mcp={"transport": "stdio"},
        a2a={"transport": "http", "port_strategy": "ephemeral"},
        capabilities_hint=["tool"], model_override=None,
    )

    host = MCPHost(hmac_key="test-key")
    await host.spawn(card)
    try:
        tools = await host.list_tools("tool-agent")
        assert "read_file" in [t.name for t in tools]
    finally:
        await host.shutdown_all()
