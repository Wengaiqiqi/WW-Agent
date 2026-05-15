from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from agents.shared.mcp_server import ToolSpec
from agents.shared.authz import verify_grant


def _skills_root() -> Path:
    return Path("skills")


def _load_skill_md(slug: str) -> str:
    return (_skills_root() / slug / "SKILL.md").read_text(encoding="utf-8")


def build_skill_specs() -> list[ToolSpec]:
    """Scan skills/*/SKILL.md and produce a ToolSpec for each.

    The MCP-exposed `name` follows the pattern `skill.<slug>`.
    """
    specs: list[ToolSpec] = []
    root = _skills_root()
    if not root.exists():
        return specs
    for skill_dir in sorted(root.iterdir()):
        if not skill_dir.is_dir():
            continue
        if not (skill_dir / "SKILL.md").exists():
            continue
        slug = skill_dir.name

        async def _handler(args: dict, _slug=slug) -> Any:
            return await execute_skill(_slug, args)

        specs.append(ToolSpec(
            name=f"skill.{slug}",
            description=f"Run the {slug} skill",
            input_schema={"type": "object"},
            handler=_handler,
        ))
    return specs


async def execute_skill(slug: str, args: dict, *, llm=None) -> str:
    """Execute a skill by feeding its SKILL.md + args to the LLM.

    Verifies authz_grant against AUTHZ_HMAC_KEY env. If `llm` is None,
    constructs a default model from the current env (mock provider falls
    back to a deterministic stub).
    """
    meta = args.get("_meta") or {}
    grant = meta.get("authz_grant")
    if grant is None:
        raise RuntimeError("missing authz_grant")
    key = os.environ.get("AUTHZ_HMAC_KEY")
    if not key:
        raise RuntimeError("AUTHZ_HMAC_KEY not set")
    verify_grant(grant, key=key, requested_tool=f"skill.{slug}")

    if llm is None:
        llm = _default_llm()

    skill_md = _load_skill_md(slug)
    user_payload = {k: v for k, v in args.items() if k != "_meta"}
    messages = [
        {"role": "system", "content": skill_md},
        {"role": "user", "content": str(user_payload)},
    ]
    result = llm.invoke(messages)
    return result.content


def _default_llm():
    """Construct the per-agent LLM. For Day-1 we only support the mock provider
    in this code path; real provider construction will be added when we wire
    the LLM planner in Task 6.3."""
    from agents.shared.mock_chat_model import MockChatModel
    provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "mock/mock-default")
    if provider.startswith("mock"):
        return MockChatModel(responses=["(mock skill output)"])
    # For real providers, defer to legacy chat-model factory.
    # If the legacy module exposes a `_build_chat_model` (or equivalent) we use it.
    # If not, raise — Task 6.3 will replace this stub with the proper factory.
    raise RuntimeError(
        f"skill-agent's per-process LLM factory only supports 'mock' in Day-1; "
        f"got LANGCHAIN_AGENT_MODEL={provider!r}. Pass an explicit `llm=` to "
        f"execute_skill or wait for Task 6.3 to wire the real factory."
    )
