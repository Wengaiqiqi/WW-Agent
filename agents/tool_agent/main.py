"""tool-agent process entrypoint.

Launched by orchestrator via:
    python -m agents.tool_agent.main
"""
from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from agents.shared.mcp_server import build_server, ToolSpec
from agents.shared.a2a_server import A2AServer, A2AHandler
from agents.tool_agent.tool_executor import build_tool_specs, execute_tool


async def amain() -> None:
    # 1. Start A2A server first so we know our bound URL.
    async def a2a_dispatch(skill_id: str, input: dict, meta: dict) -> dict:
        """A2A tasks/send dispatcher for tool-agent.
        skill_id is like 'tool.read_file' -> execute as the underlying tool."""
        if not skill_id.startswith("tool."):
            return {"error": f"tool-agent does not expose {skill_id}"}
        tool_name = skill_id[len("tool."):]

        # Record this A2A invocation so the orchestrator can see it in the unified
        # stream, even though it bypasses the orchestrator → specialist MCP path.
        from orchestrator.telemetry import emit_event
        emit_event(
            agent_id="tool-agent",
            trace_id=meta.get("trace_id", "?"),
            message=f"(via A2A from {meta.get('agent_caller', '?')}) {skill_id}",
        )

        args = {**input, "_meta": meta}
        result = await execute_tool(tool_name, args)
        return {"result": result}

    a2a = A2AServer(handler=A2AHandler(handler=a2a_dispatch))
    await a2a.start()

    # 2. Write the A2A URL to the runtime dir so orchestrator can discover it.
    agent_id = os.environ.get("AGENT_ID", "tool-agent")
    runtime_dir = Path(".agent/runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"{agent_id}.a2a-url").write_text(a2a.base_url, encoding="utf-8")

    # 3. Build the MCP server (existing logic).
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

    try:
        await runner()
    finally:
        await a2a.stop()


def main() -> int:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
