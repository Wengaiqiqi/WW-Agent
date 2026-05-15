"""Thin wrapper around the official MCP SDK's stdio server.

Both skill-agent and tool-agent use this. They differ only in the list of
`ToolSpec` they register.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp.server import Server
from mcp.types import TextContent, Tool


Handler = Callable[[dict], Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Handler


class _ServerProxy:
    """Wraps the MCP `Server` and remembers the spec list for direct testing."""

    def __init__(self, server: Server, specs: list[ToolSpec]):
        self._server = server
        self._specs = {s.name: s for s in specs}

    async def list_tools_impl(self) -> list[Tool]:
        return [
            Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
            for s in self._specs.values()
        ]

    async def call_tool_impl(self, name: str, arguments: dict) -> Any:
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(f"unknown tool: {name}")
        return await spec.handler(arguments)

    @property
    def server(self) -> Server:
        return self._server


def build_server(*, name: str, tools: list[ToolSpec]) -> tuple[_ServerProxy, Any]:
    """Construct an MCP Server with the given ToolSpecs registered.

    Returns (proxy, stdio_runner) where `stdio_runner` is an async function
    the specialist's main() will await to serve over stdio.

    SDK notes (mcp==1.16.0):
    - `server.list_tools()` is called with parens and returns a decorator.
    - `server.call_tool()` handler receives (tool_name: str, arguments: dict).
    - `Tool` uses camelCase `inputSchema`.
    - `stdio_server` lives at `mcp.server.stdio`.
    """
    server = Server(name)
    spec_map = {s.name: s for s in tools}

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
            for s in spec_map.values()
        ]

    @server.call_tool()
    async def _call_tool(tool_name: str, arguments: dict) -> Any:
        # SDK passes (tool_name, arguments) as two positional args.
        spec = spec_map.get(tool_name)
        if spec is None:
            raise ValueError(f"unknown tool: {tool_name}")
        result = await spec.handler(arguments)
        # The MCP SDK treats any iterable (including str) as a sequence of
        # content blocks. A bare string would be iterated character by character.
        # Wrap string results in a TextContent list so the SDK is happy.
        if isinstance(result, str):
            return [TextContent(type="text", text=result)]
        return result

    proxy = _ServerProxy(server, tools)

    async def stdio_runner() -> None:
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    return proxy, stdio_runner
