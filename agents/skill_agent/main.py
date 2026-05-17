"""skill-agent process entrypoint.

Launched by orchestrator via:
    python -m agents.skill_agent.main

Exposes two surfaces, mirroring tool-agent:

* MCP stdio  — one ToolSpec per ``skill.<slug>``; backward-compatible with
  the orchestrator's MCP dispatch path.
* A2A HTTP   — both an RPC handler (``/a2a``) for legacy non-streaming
  callers AND a streaming handler (``/a2a/stream``) so the orchestrator's
  ``_delegate_to_agent`` can render skill progress (text / tool_call /
  tool_result / done) live in the TUI.

The streaming dispatcher reads the target skill slug from
``meta["skill_slug"]`` so the orchestrator can route ``skill.<slug>``
capabilities through the same A2A entry point as ``tool.task``.
"""
from __future__ import annotations
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator

from agents.shared.mcp_server import build_server, ToolSpec
from agents.shared.a2a_server import A2AServer, A2AHandler, A2AStreamHandler
from agents.skill_agent.skill_executor import (
    build_skill_specs,
    execute_skill,
    execute_skill_streaming,
)

log = logging.getLogger(__name__)


def _slug_from_meta(meta: dict, task: str) -> str | None:
    """Pick the skill slug for a streaming dispatch.

    Priority:
    1. ``meta["skill_slug"]`` set by the orchestrator (preferred).
    2. ``meta["skill_id"]`` if the caller used the legacy field name.
    3. Inferred from the task text via a ``skill.<slug>`` prefix.
    """
    raw = meta.get("skill_slug") or meta.get("skill_id") or ""
    if isinstance(raw, str) and raw.startswith("skill."):
        raw = raw[len("skill."):]
    if raw:
        return str(raw)
    # Fallback: "<slug>:<rest>" or "skill.<slug>: <rest>" prefix in task.
    if isinstance(task, str) and task:
        if task.startswith("skill."):
            head, _, _ = task[len("skill."):].partition(":")
            head = head.strip()
            if head:
                return head
    return None


async def amain() -> None:
    # --- Non-streaming A2A dispatch (legacy ``call_peer`` path) -----------
    async def a2a_dispatch(skill_id: str, input: dict, meta: dict) -> dict:
        if not skill_id.startswith("skill."):
            return {"error": f"skill-agent does not expose {skill_id}"}
        slug = skill_id[len("skill."):]
        args = {**input, "_meta": meta}
        try:
            result = await execute_skill(slug, args)
        except Exception as exc:
            log.exception("a2a_dispatch failed for %s", skill_id)
            return {"error": str(exc)}
        return {"result": result}

    # --- Streaming A2A dispatch for orchestrator's _delegate_to_agent -----
    async def a2a_stream_dispatch(payload: dict) -> AsyncIterator[dict[str, Any]]:
        task = payload.get("task") or ""
        meta = payload.get("meta") or {}
        slug = _slug_from_meta(meta, task)
        if not slug:
            yield {
                "type": "error",
                "message": (
                    "skill streaming dispatch needs `meta.skill_slug` (e.g. "
                    "\"baidu-ecommerce-search\") to know which skill to run."
                ),
            }
            return

        # Compose args: the user's task becomes the "query" so SKILL.md's
        # context + envelope protocol know what's being asked.
        args = {"query": task, "_meta": meta}
        async for event in execute_skill_streaming(slug, args):
            yield event

    a2a = A2AServer(
        handler=A2AHandler(handler=a2a_dispatch),
        stream_handler=A2AStreamHandler(handler=a2a_stream_dispatch),
    )
    await a2a.start()

    agent_id = os.environ.get("AGENT_ID", "skill-agent")
    runtime_dir = Path(".agent/runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"{agent_id}.a2a-url").write_text(a2a.base_url, encoding="utf-8")

    # --- MCP stdio surface ------------------------------------------------
    specs = build_skill_specs()

    def _make_handler(skill_name: str):
        """Wrap each skill so MCP dispatch routes through execute_skill (uniform authz)."""
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
