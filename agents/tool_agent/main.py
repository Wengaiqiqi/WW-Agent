"""tool-agent process entrypoint.

Launched by orchestrator via:
    python -m agents.tool_agent.main
"""
from __future__ import annotations
import asyncio
import sys
from typing import Any
from agents.shared.mcp_server import build_server, ToolSpec
from agents.tool_agent.tool_executor import build_tool_specs, execute_tool


async def amain() -> None:
    specs = build_tool_specs()

    def _make_handler(tool_name: str):
        async def _h(args: dict) -> Any:
            return await execute_tool(tool_name, args)
        return _h

    guarded = [
        ToolSpec(s.name, s.description, s.input_schema, _make_handler(s.name))
        for s in specs
    ]
    _proxy, runner = build_server(name="tool-agent", tools=guarded)
    await runner()


def main() -> int:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
