import asyncio
from agents.shared.mcp_server import build_server, ToolSpec


def test_build_server_registers_tools():
    async def handler(args):
        return {"echo": args.get("x")}

    spec = ToolSpec(
        name="echo",
        description="Echo back x",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        handler=handler,
    )
    server, _stdio = build_server(name="test-agent", tools=[spec])
    tools = asyncio.run(server.list_tools_impl())
    names = [t.name for t in tools]
    assert "echo" in names


def test_call_tool_dispatches_to_handler():
    async def handler(args):
        return {"echo": args.get("x")}

    spec = ToolSpec(
        name="echo", description="", input_schema={}, handler=handler,
    )
    server, _ = build_server(name="test-agent", tools=[spec])
    result = asyncio.run(server.call_tool_impl("echo", {"x": "hi"}))
    assert result == {"echo": "hi"}
