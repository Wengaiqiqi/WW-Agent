"""Shared display helpers for rendering tool calls in the REPL.

Three pieces of cross-surface knowledge live here so legacy single-agent,
multi-agent orchestrator, and tool-agent's ReAct loop don't each grow their
own copy:

* ``TOOL_ARG_PRIMARY_KEY`` — the "main" argument to surface in a one-line
  tool-call header. ``run_command``'s primary arg is ``command``, not the
  optional ``timeout``; ``read_file``'s is ``path``; etc. Without this map
  the header would either dump every kwarg or pick one arbitrarily.
* ``has_raw_tool_markup`` — detects ``<tool_call>`` / ``<function=`` /
  ``<parameter=`` strings that some models occasionally emit as *content*
  instead of going through the tool-calling API. Used to suppress those
  segments before they reach the terminal.
* ``is_langgraph_tool_chunk`` — recognises langgraph stream chunks whose
  ``.type`` (or class name) names them as a tool message, so the renderer
  skips them in the prose stream.

Each helper was previously inlined in 2-3 places with subtle drift
(``"memory": "operation"`` vs. the actual ``memory(action=...)`` signature,
missing entries for newer tools, slightly different markup lists). One
source of truth.
"""
from __future__ import annotations


# Mapping of tool-name → the argument key to display next to the tool name
# in a streaming tool-call header (e.g. ``⏺ read_file  README.md``).
#
# When a tool is invoked the renderer pulls ``args[TOOL_ARG_PRIMARY_KEY[name]]``;
# if the tool isn't in this map (or the key is missing from args) the
# renderer falls back to a generic ``k=v`` summary of the first few args.
TOOL_ARG_PRIMARY_KEY: dict[str, str] = {
    # file ops
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "list_directory": "path",
    "glob_search": "pattern",
    "grep_search": "pattern",
    # multi-file patch
    "apply_patch": "patch",
    # subprocess
    "run_command": "command",
    "run_python": "code",
    # web
    "web_search": "query",
    "web_extract": "url",
    "web_crawl": "url",
    # other
    "memory": "action",   # the @tool signature is (action, target, content, old_text)
    "clarify": "question",
    "calculator": "expression",
    # hermes-agent ports
    "osv_check": "package",
    "home_assistant": "action",
    "x_search": "query",
    "vision_analyze": "image",
    "mixture_of_agents": "user_prompt",
}


_RAW_TOOL_MARKERS = ("<tool_call>", "<function=", "<parameter=")


def has_raw_tool_markup(content: str) -> bool:
    """True when *content* contains raw tool-calling markup as plain text.

    Some models (notably a few local llama-style quants and a couple of
    older Qwen variants) sometimes emit the underlying tool-call protocol
    as content tokens instead of routing it through the structured tool-
    calling API. The renderer treats these strings as garbage and drops
    them from the user-facing stream.
    """
    return any(m in content for m in _RAW_TOOL_MARKERS)


def is_langgraph_tool_chunk(chunk: object) -> bool:
    """Recognise a stream chunk that langgraph tagged as a tool message.

    Used by both the legacy single-agent loop and tool-agent's ReAct loop
    to filter out tool messages from the *prose* stream — they're already
    rendered separately as ``⏺ tool`` headers + result panels.

    Defensive: handles both pre-Pydantic and Pydantic chunk shapes, and
    falls back to substring-matching the class name for chunk types we
    haven't seen yet.
    """
    chunk_type = getattr(chunk, "type", "")
    if chunk_type in {"tool", "ToolMessage", "tool_message"}:
        return True
    return "tool" in chunk.__class__.__name__.lower()


def extract_message_text(content: object) -> str:
    """Return user-visible text from a LangChain message content value.

    Providers may return a plain string or a list of typed content blocks.
    Only ``text`` blocks belong in the rendered answer; reasoning, signatures,
    tool payloads, and other provider metadata must stay out of the UI.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return "" if content is None else str(content)

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if isinstance(block, dict):
            block_type = block.get("type")
            text = block.get("text")
        else:
            block_type = getattr(block, "type", None)
            text = getattr(block, "text", None)
        if block_type in (None, "text") and isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def format_tool_arg_summary(name: str, args: dict, *, max_width: int = 80) -> str:
    """Produce a single-line argument summary for a tool-call header.

    Prefers the primary key from ``TOOL_ARG_PRIMARY_KEY``; falls back to
    a ``k=v, k=v`` summary of the first two args. Long values are
    truncated with an ellipsis.
    """
    if not args:
        return ""
    key = TOOL_ARG_PRIMARY_KEY.get(name)
    if key and key in args and args[key] is not None:
        value = str(args[key])
        first_line = value.strip().splitlines()[0] if value.strip() else ""
        if len(first_line) > max_width:
            first_line = first_line[: max_width - 3] + "…"
        return first_line
    pairs = []
    for k, v in list(args.items())[:2]:
        v_str = v if isinstance(v, str) else repr(v)
        if len(v_str) > 40:
            v_str = v_str[:37] + "…"
        pairs.append(f"{k}={v_str}")
    return ", ".join(pairs)
