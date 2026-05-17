"""Skill-agent execution loop.

A skill is a directory under ``skills/<slug>/`` with a ``SKILL.md`` that
describes its domain (which APIs to call, in what order, with what flags).
The skill-agent loads ``SKILL.md`` as the system prompt and runs a small
ReAct-style loop:

1. Render system message = protocol header + tools list + SKILL.md content.
2. Call the LLM with [system, user_payload, ...tool_results].
3. Parse the model's reply as a JSON envelope:
     * ``{"tool_calls": [...]}``  → invoke each on tool-agent via A2A.
     * ``{"final": "..."}``       → that's the answer, return it.
     * non-JSON / unparseable → treat as the final answer.
4. Iterate until ``final`` or the iteration cap (12).

Compared with the day-1 stub this module now:

* Builds a real LLM via ``config.build_llm`` (was: only mock).
* Tells the model the envelope protocol explicitly in the system prompt
  (was: SKILL.md alone, so non-mock models never emitted tool_calls).
* Tolerant JSON parse (strips markdown code fences, extracts the first
  balanced ``{ ... }`` if the model added prose around it).
* Streams events (text / tool_call / tool_result / done / error) so the
  orchestrator's TUI can render progress live, matching tool-agent's UX.
* Surfaces a diagnostic when the loop exits without a ``final`` so the
  user never sees an empty turn.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator

from agents.shared.mcp_server import ToolSpec
from agents.shared.authz import verify_grant

logger = logging.getLogger(__name__)


def _skills_root() -> Path:
    return Path("skills")


def _load_skill_md(slug: str) -> str:
    return (_skills_root() / slug / "SKILL.md").read_text(encoding="utf-8")


def _load_skill_description(slug: str) -> str:
    """Best-effort extract of the ``description:`` line from SKILL.md frontmatter.

    Falls back to the first non-frontmatter heading, then to a generic label
    so the planner always has *something* to display.
    """
    try:
        content = _load_skill_md(slug)
    except OSError:
        return f"Run the {slug} skill"

    lines = content.splitlines()
    in_frontmatter = False
    for line in lines:
        s = line.strip()
        if s == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter and s.lower().startswith("description:"):
            desc = s.split(":", 1)[1].strip().strip('"').strip("'")
            if desc:
                return desc

    for line in lines:
        s = line.strip()
        if s.startswith("#"):
            heading = s.lstrip("#").strip()
            if heading:
                return f"Run the {slug} skill: {heading}"
        elif s:
            return f"Run the {slug} skill: {s[:160]}"

    return f"Run the {slug} skill"


def build_skill_specs() -> list[ToolSpec]:
    """Scan ``skills/*/SKILL.md`` and produce a ToolSpec for each.

    The MCP-exposed name is ``skill.<slug>``. The description is pulled from
    SKILL.md's frontmatter so the planner has a real signal about when to
    route a request here (the previous ``Run the {slug} skill`` told the
    planner nothing).
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

        specs.append(
            ToolSpec(
                name=f"skill.{slug}",
                description=_load_skill_description(slug),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Free-form user request that the skill should fulfill. "
                                "The skill's SKILL.md is loaded as the system prompt and "
                                "the model decides which scripts/APIs to invoke."
                            ),
                        },
                    },
                },
                handler=_handler,
            )
        )
    return specs


# ---------------------------------------------------------------------------
# Protocol header — the LLM contract for the JSON envelope.
# ---------------------------------------------------------------------------

_TOOL_CATALOG = """\
- read_file(path) → JSON {filePath, content, numLines, ...}
- write_file(path, content) → JSON {filePath, content}
- list_directory(path) → JSON directory listing
- glob_search(pattern) → JSON list of matching paths
- grep_search(pattern, path, glob_pattern, output_mode, ...) → JSON matches
- web_search(query, limit, provider) → JSON {results:[{title,url,snippet}]}
- web_extract(url, max_chars) → JSON {title, url, text}
- web_crawl(url, max_pages, ...) → JSON {pages:[{url,title,text}]}
- run_python(code, timeout) → JSON {stdout, stderr, exitCode}
- run_command(command, timeout) → JSON {stdout, stderr, exitCode}
"""

_PROTOCOL_HEADER = f"""\
You are running inside a SKILL-AGENT. You execute a single domain skill by
calling tools on a peer TOOL-AGENT via JSON envelopes.

## Output protocol
Every reply MUST be ONE JSON object — no prose, no markdown fences. Pick one:

1) Call tools:
   {{"tool_calls": [{{"tool": "<name>", "arguments": {{<arg-key>: <value>, ...}}}}]}}

2) Give the final answer to the user:
   {{"final": "<plain-text answer>"}}

After each `tool_calls` reply you will receive a user-role message that
starts with `[Tool results]` and contains a JSON array
`[{{"tool": "<name>", "output": <whatever>}}, ...]`. Read it, decide the
next step, and reply again with ONE envelope.

## Tools available on the peer tool-agent
{_TOOL_CATALOG}
## Termination rules
- Do not call the same failing tool more than twice with the same arguments.
- Hard cap: ~10 tool calls. After that, reply with `{{"final": "..."}}` even
  if the answer is partial — explain what you tried.
- The final answer text must be plain text (no JSON).

## Skill instructions (domain-specific)
"""


def _build_system_message(skill_md: str) -> str:
    return _PROTOCOL_HEADER + skill_md


# ---------------------------------------------------------------------------
# Tolerant JSON envelope parsing.
# ---------------------------------------------------------------------------


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    nl = text.find("\n")
    if nl == -1:
        return text
    body = text[nl + 1 :]
    if body.endswith("```"):
        body = body[: -3]
    return body.strip()


def _extract_first_json_object(text: str) -> str | None:
    """Find the first balanced ``{ ... }`` block in *text*.

    Skips string-literal contents so braces inside JSON strings don't trip
    the balance counter. Returns ``None`` if no balanced object is found.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    i = start
    in_str = False
    escape = False
    while i < len(text):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _parse_envelope(text: str) -> dict | None:
    """Best-effort parse of the model's reply into an envelope dict.

    Returns ``None`` if no JSON object can be salvaged — callers treat that
    as "the model gave a plain-text final answer".
    """
    body = _strip_code_fences(text)
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    candidate = _extract_first_json_object(body)
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Execution loop — streaming + non-streaming entry points.
# ---------------------------------------------------------------------------

MAX_ITERATIONS = 12


async def execute_skill(slug: str, args: dict, *, llm=None) -> str:
    """Non-streaming entry point — collect streaming events, return final text.

    Kept for backward compatibility with the MCP path (which expects a string
    result) and existing unit tests.
    """
    final_text = ""
    async for event in execute_skill_streaming(slug, args, llm=llm):
        etype = event.get("type", "")
        if etype == "done":
            final_text = event.get("text", "")
            break
        if etype == "error":
            raise RuntimeError(event.get("message", "skill error"))
    return final_text


async def execute_skill_streaming(
    slug: str, args: dict, *, llm=None
) -> AsyncIterator[dict[str, Any]]:
    """Run the skill loop, yielding orchestrator-compatible events.

    Event types match tool-agent's: thinking / text / tool_call / tool_result
    / done / error. The orchestrator's ``_delegate_to_agent`` consumes them
    identically.
    """
    meta = args.get("_meta") or {}
    grant = meta.get("authz_grant")
    if grant is None:
        yield {"type": "error", "message": "missing authz_grant"}
        return
    key = os.environ.get("AUTHZ_HMAC_KEY")
    if not key:
        yield {"type": "error", "message": "AUTHZ_HMAC_KEY not set"}
        return
    try:
        verify_grant(grant, key=key, requested_tool=f"skill.{slug}")
    except Exception as exc:
        yield {"type": "error", "message": f"authz: {exc}"}
        return

    try:
        skill_md = _load_skill_md(slug)
    except OSError as exc:
        yield {"type": "error", "message": f"could not load skill {slug!r}: {exc}"}
        return

    if llm is None:
        try:
            llm = _default_llm()
        except Exception as exc:
            yield {"type": "error", "message": f"could not build LLM: {exc}"}
            return

    user_payload = {k: v for k, v in args.items() if k != "_meta"}
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _build_system_message(skill_md)},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)
                                    if isinstance(user_payload, dict) else str(user_payload)},
    ]

    tool_calls_count = 0
    final_text: str = ""
    terminal_answer_seen = False

    yield {"type": "thinking"}

    for iteration in range(MAX_ITERATIONS):
        try:
            response = await _invoke_llm(llm, messages)
        except Exception as exc:
            logger.exception("skill LLM invocation failed (iter %d)", iteration)
            partial = final_text.strip()
            diag = _no_final_diagnostic(tool_calls_count, reason="interrupted")
            if partial:
                yield {"type": "text", "chunk": "\n\n" + diag}
                yield {
                    "type": "done",
                    "text": partial + "\n\n" + diag,
                    "tool_calls": tool_calls_count,
                }
            else:
                yield {"type": "error", "message": f"skill LLM error: {exc}"}
            return

        text = _extract_text(response)
        envelope = _parse_envelope(text)

        if envelope is None or ("tool_calls" not in envelope and "final" not in envelope):
            # Non-envelope reply → treat the whole thing as the final answer.
            answer = text.strip()
            if answer:
                yield {"type": "text", "chunk": answer}
                yield {"type": "done", "text": answer, "tool_calls": tool_calls_count}
                return
            # Truly empty model reply — diagnose and stop.
            diag = _no_final_diagnostic(tool_calls_count, reason="empty_reply")
            yield {"type": "text", "chunk": diag}
            yield {"type": "done", "text": diag, "tool_calls": tool_calls_count}
            return

        if "final" in envelope:
            answer = str(envelope.get("final") or "").strip()
            terminal_answer_seen = True
            final_text = answer
            if answer:
                yield {"type": "text", "chunk": answer}
            yield {"type": "done", "text": answer, "tool_calls": tool_calls_count}
            return

        tool_calls = envelope.get("tool_calls") or []
        if not isinstance(tool_calls, list) or not tool_calls:
            # Empty tool_calls list — treat as no-op and ask the model again,
            # but log so we don't spin silently.
            logger.warning("skill emitted empty tool_calls list at iter %d", iteration)
            messages = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": "Your previous reply had an empty tool_calls list. "
                               "Emit either non-empty tool_calls or {\"final\": \"...\"}.",
                },
            ]
            continue

        tool_outputs: list[dict[str, Any]] = []
        for call in tool_calls:
            tool_name = str(call.get("tool") or "").strip()
            arguments = call.get("arguments") or {}
            if not tool_name:
                tool_outputs.append({"tool": "?", "error": "missing 'tool' field"})
                continue
            yield {"type": "tool_call", "name": tool_name, "args": arguments}
            try:
                output = await _call_remote_tool(tool_name, arguments, meta)
            except Exception as exc:
                logger.warning("skill tool %s failed: %s", tool_name, exc)
                output = {"error": str(exc)}
            tool_calls_count += 1
            yield {
                "type": "tool_result",
                "name": tool_name,
                "preview": _truncate_preview(output),
            }
            tool_outputs.append({"tool": tool_name, "output": output})

        # Feed results back as a user-role message. We deliberately do NOT
        # use ``role: tool`` here: that role is part of OpenAI's native
        # function-calling protocol and the API requires every such message
        # to carry a ``tool_call_id`` referencing an ``assistant.tool_calls``
        # entry. Our JSON-envelope protocol emits the call list inside
        # ``assistant.content`` (not in ``tool_calls``), so no id exists.
        # langchain-openai's dict→ToolMessage converter then KeyErrors on
        # ``tool_call_id`` and the whole skill turn aborts.
        messages = messages + [
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": "[Tool results]\n"
                + json.dumps(tool_outputs, ensure_ascii=False),
            },
        ]

    # Iteration cap exhausted without a `final`. Surface the diagnostic.
    diag = _no_final_diagnostic(tool_calls_count, reason="iteration_cap")
    if final_text.strip():
        yield {"type": "text", "chunk": "\n\n" + diag}
        yield {
            "type": "done",
            "text": final_text.strip() + "\n\n" + diag,
            "tool_calls": tool_calls_count,
        }
    else:
        yield {"type": "text", "chunk": diag}
        yield {"type": "done", "text": diag, "tool_calls": tool_calls_count}


def _no_final_diagnostic(tool_calls_count: int, *, reason: str) -> str:
    call_word = "tool call" if tool_calls_count == 1 else "tool calls"
    if reason == "iteration_cap":
        return (
            f"_(Skill made {tool_calls_count} {call_word} but did not reach a final "
            f"answer within the iteration cap. Try a more focused request.)_"
        )
    if reason == "interrupted":
        return (
            f"_(Skill was interrupted after {tool_calls_count} {call_word} before it "
            f"could write a final answer. Try again or rephrase.)_"
        )
    return (
        f"_(Skill model returned an empty reply after {tool_calls_count} {call_word}. "
        f"Check the model/provider configuration.)_"
    )


def _extract_text(response: Any) -> str:
    raw = getattr(response, "content", response)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for chunk in raw:
            if isinstance(chunk, dict):
                txt = chunk.get("text") or chunk.get("content")
                if txt:
                    parts.append(str(txt))
            elif chunk is not None:
                parts.append(str(chunk))
        return "".join(parts)
    if raw is None:
        return ""
    return str(raw)


def _truncate_preview(output: Any, max_len: int = 200) -> str:
    if isinstance(output, str):
        s = output
    else:
        try:
            s = json.dumps(output, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(output)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


# ---------------------------------------------------------------------------
# LLM invocation — small wrapper so we can keep both sync and async LLMs.
# ---------------------------------------------------------------------------


async def _invoke_llm(llm, messages: list[dict[str, str]]) -> Any:
    ainvoke = getattr(llm, "ainvoke", None)
    if callable(ainvoke):
        return await ainvoke(messages)
    # Run sync .invoke on a worker thread to keep the event loop responsive.
    import asyncio

    return await asyncio.to_thread(llm.invoke, messages)


# ---------------------------------------------------------------------------
# Remote tool call — mints a tool-specific JWT and dispatches to tool-agent.
# ---------------------------------------------------------------------------


# Tools that are useful for skills but also high-impact — we log every
# grant so the user can audit what a skill did after the fact.
_AUDITED_INNER_TOOLS = frozenset({"run_command", "run_python", "write_file",
                                   "edit_file", "apply_patch"})


def _mint_tool_grant(tool_name: str, meta: dict) -> str:
    """Mint a short-lived JWT granting access to a specific tool on tool-agent.

    Uses the *inner* whitelist (``_SKILL_INNER_WHITELIST``) rather than the
    *outer* one (``_MODE_WHITELIST``). The two answer different questions:

    * Outer (``_MODE_WHITELIST``): "what can the planner DIRECTLY dispatch
      from a user message?" — restrictive, so workspace-write users don't
      accidentally get a shell.
    * Inner (``_SKILL_INNER_WHITELIST``): "what can an ALREADY-ALLOWED skill
      call to do its job?" — permissive, because the user invoked the skill
      by name and curated skills under ``skills/<slug>/`` legitimately need
      to shell out to their own domain scripts.

    Skills under ``read-only`` are blocked upstream by the orchestrator's
    PermissionGate, so this function only ever sees workspace-write or
    danger-full-access in practice — but we keep the read-only entry for
    defence-in-depth.
    """
    import time
    import jwt as pyjwt
    from agents.shared.permission_modes import (
        _SKILL_INNER_WHITELIST,
        PermissionDenied,
    )

    inherited_mode = meta.get("permission_mode", "workspace-write")
    wl = _SKILL_INNER_WHITELIST.get(inherited_mode, [])
    if "*" not in wl and tool_name not in wl:
        raise PermissionDenied(
            f"skill attempted to mint grant for {tool_name!r}, but the user's "
            f"mode {inherited_mode!r} does not permit any skill execution. "
            f"Skills are disabled under read-only; switch to workspace-write."
        )

    if tool_name in _AUDITED_INNER_TOOLS:
        logger.info(
            "skill-grant: tool=%s mode=%s trace=%s",
            tool_name, inherited_mode, meta.get("trace_id", "?"),
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


async def _call_remote_tool(tool_name: str, arguments: dict, meta: dict) -> Any:
    """Call ``tool.<tool_name>`` on the peer tool-agent via A2A."""
    from agents.skill_agent.a2a_client import call_peer

    tool_grant = _mint_tool_grant(tool_name, meta)
    tool_meta = {**meta, "authz_grant": tool_grant, "agent_caller": "skill-agent"}
    out = await call_peer(
        peer_id="tool-agent",
        skill_id=f"tool.{tool_name}",
        input=arguments,
        meta=tool_meta,
    )
    if isinstance(out, dict) and "result" in out:
        return out["result"]
    return out


# ---------------------------------------------------------------------------
# Default LLM — real provider or mock.
# ---------------------------------------------------------------------------


def _default_llm():
    """Build an LLM from the active config; fall back to a mock when requested.

    Mock path: when ``LANGCHAIN_AGENT_MODEL`` starts with ``mock`` (the test
    harness sets this), return a ``MockChatModel`` driven by
    ``MOCK_SKILL_SCRIPT``.

    Real path: hydrate credentials from the project's config and call
    ``config.build_llm``. Any failure raises — callers surface a clean
    error event to the orchestrator instead of a silent fallback.
    """
    raw = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
    if raw.startswith("mock"):
        from agents.shared.mock_chat_model import MockChatModel

        return MockChatModel.from_env(
            "MOCK_SKILL_SCRIPT",
            default='{"final":"(mock skill output)"}',
        )

    try:
        from config import build_llm, hydrate_env_from_credentials, load_active_config
    except Exception as exc:  # pragma: no cover - import-time misconfig
        raise RuntimeError(
            f"skill-agent could not import config (project not installed?): {exc}"
        ) from exc

    hydrate_env_from_credentials()
    return build_llm(load_active_config())
