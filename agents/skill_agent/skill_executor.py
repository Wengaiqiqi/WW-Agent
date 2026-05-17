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
    """Execute a skill via a simple ReAct loop:
      - LLM is prompted with SKILL.md + user payload + accumulated tool results
      - If LLM returns {"tool_calls":[...]}, call each via A2A and re-prompt
      - If LLM returns {"final":"..."} or plain text, return that as final answer
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
    return await _react_loop(messages, llm, meta)


def _mint_tool_grant(tool_name: str, meta: dict) -> str:
    """Mint a short-lived JWT granting access to a specific tool on tool-agent.

    Skill-agent holds AUTHZ_HMAC_KEY so it can issue sub-grants for tools it
    needs to invoke downstream. CRITICAL: the inner tool MUST be one the
    user's outer mode would have allowed — otherwise a skill could side-step
    the gate by simply asking for ``run_command`` even when the user set
    ``read-only``. We re-validate against the same _MODE_WHITELIST the
    orchestrator's PermissionGate uses.
    """
    import time
    import jwt as pyjwt
    from agents.shared.permission_modes import _MODE_WHITELIST, PermissionDenied

    inherited_mode = meta.get("permission_mode", "workspace-write")
    wl = _MODE_WHITELIST.get(inherited_mode, [])
    if "*" not in wl and tool_name not in wl:
        raise PermissionDenied(
            f"skill attempted to mint grant for {tool_name!r}, but the user's "
            f"mode {inherited_mode!r} does not permit it. Re-run with a higher "
            f"permission mode if this is intentional."
        )

    key = os.environ.get("AUTHZ_HMAC_KEY", "")
    now = int(time.time())
    payload = {
        "iss": "skill-agent",
        "sub": "tool-agent",
        "exp": now + 60,
        "permission_mode": inherited_mode,
        "allowed_tools": [tool_name],
        "trace_id": meta.get("trace_id", ""),
    }
    return pyjwt.encode(payload, key, algorithm="HS256")


async def _react_loop(messages, llm, meta, max_iters: int = 5):
    """Iterate: LLM → optional tool_calls (A2A) → re-prompt → final."""
    from agents.skill_agent.a2a_client import call_peer
    import json

    for _ in range(max_iters):
        result = llm.invoke(messages)
        text = result.content.strip()

        # Try parsing the envelope. Non-JSON or non-envelope responses are
        # treated as the final answer.
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            return text

        if not isinstance(envelope, dict):
            return text

        calls = envelope.get("tool_calls") or []
        if not calls:
            return envelope.get("final", text)

        tool_outputs = []
        for c in calls:
            tool = c["tool"]
            arguments = c.get("arguments") or {}
            # Mint a fresh grant specifically for this tool so tool-agent's
            # authz check passes (the original grant only covers skill.X).
            tool_grant = _mint_tool_grant(tool, meta)
            tool_meta = {**meta, "authz_grant": tool_grant, "agent_caller": "skill-agent"}
            out = await call_peer(
                peer_id="tool-agent",
                skill_id=f"tool.{tool}",
                input=arguments,
                meta=tool_meta,
            )
            tool_outputs.append({"tool": tool, "output": out})

        messages = messages + [
            {"role": "tool", "content": json.dumps(tool_outputs)}
        ]
    # If the LLM never returns final, return the last text we saw.
    return text


def _default_llm():
    from agents.shared.mock_chat_model import MockChatModel
    provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "mock/mock-default")
    if provider.startswith("mock"):
        return MockChatModel.from_env("MOCK_SKILL_SCRIPT", default='{"final":"(mock skill output)"}')
    raise RuntimeError(
        f"skill-agent's LLM factory only supports 'mock' in Day-1; got {provider!r}"
    )
