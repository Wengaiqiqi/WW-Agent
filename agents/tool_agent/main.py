"""tool-agent process entrypoint.

Launched by orchestrator via:
    python -m agents.tool_agent.main
"""
from __future__ import annotations
import asyncio
import sys
from agents.shared.mcp_server import build_server
from agents.tool_agent.tool_executor import build_tool_specs


async def amain() -> None:
    specs = build_tool_specs()
    # Wrap each handler so it verifies the authz_grant before executing.
    # (Authz wrapper added in Task 4.4 — for now handlers run unguarded.)
    proxy, runner = build_server(name="tool-agent", tools=specs)
    await runner()


def main() -> int:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
