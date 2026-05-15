"""skill-agent process entrypoint.

Launched by orchestrator via:
    python -m agents.skill_agent.main
"""
from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from agents.shared.mcp_server import build_server, ToolSpec
from agents.shared.a2a_server import A2AServer, A2AHandler
from agents.skill_agent.skill_executor import build_skill_specs, execute_skill


async def amain() -> None:
    async def a2a_dispatch(skill_id: str, input: dict, meta: dict) -> dict:
        if not skill_id.startswith("skill."):
            return {"error": f"skill-agent does not expose {skill_id}"}
        slug = skill_id[len("skill."):]
        args = {**input, "_meta": meta}
        result = await execute_skill(slug, args)
        return {"result": result}

    a2a = A2AServer(handler=A2AHandler(handler=a2a_dispatch))
    await a2a.start()

    agent_id = os.environ.get("AGENT_ID", "skill-agent")
    runtime_dir = Path(".agent/runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"{agent_id}.a2a-url").write_text(a2a.base_url, encoding="utf-8")

    specs = build_skill_specs()

    def _make_handler(skill_name: str):
        """Wrap each skill so it goes through execute_skill (uniform authz)."""
        # skill_name is "skill.<slug>"; strip the prefix to get the slug.
        slug = skill_name[len("skill."):] if skill_name.startswith("skill.") else skill_name

        async def _h(args: dict) -> Any:
            return await execute_skill(slug, args)

        return _h

    guarded = [
        ToolSpec(s.name, s.description, s.input_schema, _make_handler(s.name))
        for s in specs
    ]
    _proxy, runner = build_server(name="skill-agent", tools=guarded)

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
