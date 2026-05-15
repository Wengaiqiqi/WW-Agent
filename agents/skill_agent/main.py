"""skill-agent process entrypoint.

Launched by orchestrator via:
    python -m agents.skill_agent.main
"""
from __future__ import annotations
import asyncio
import sys
from typing import Any
from agents.shared.mcp_server import build_server, ToolSpec
from agents.skill_agent.skill_executor import build_skill_specs, execute_skill


async def amain() -> None:
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
    await runner()


def main() -> int:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
