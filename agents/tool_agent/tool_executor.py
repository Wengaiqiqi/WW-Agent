"""Bridge between existing in-process tool registry and MCP ToolSpec API.

Reuses tool/*.py functions where their signatures are trivially wrappable.
This module only adapts signatures and produces JSON schemas for MCP
``tools/list``.

File-path handling uses ``tool/tool_file_ops.resolve_workspace_path`` so a
prompt-injected ``write_file path="/etc/passwd"`` is refused at the wrapper
boundary, matching the README's "workspace-boundary checks" promise. The
sandbox can be widened in tests / on dev hosts by setting
``LANGCHAIN_AGENT_WORKSPACE_ROOT``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from agents.shared.authz import verify_grant, AuthzError
from agents.shared.mcp_server import ToolSpec


def _hmac_key() -> str:
    key = os.environ.get("AUTHZ_HMAC_KEY")
    if not key:
        raise RuntimeError("AUTHZ_HMAC_KEY env var not set; orchestrator must spawn this process")
    return key

# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------


def _do_read_file(path_arg: str) -> str:
    from tool.tool_file_ops import resolve_workspace_path

    path = resolve_workspace_path(path_arg)
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    return json.dumps(
        {
            "type": "text",
            "file": {
                "filePath": str(path),
                "content": content,
                "numLines": len(lines),
                "startLine": 1,
                "totalLines": len(lines),
            },
        },
        ensure_ascii=False,
        indent=2,
    )


async def _wrap_read_file(args: dict) -> Any:
    # File reads are usually fast, but a 50 MB file or a network FS can stall
    # the asyncio event loop for tens of ms — which blocks orchestrator SSE
    # heartbeats and the agent's own ``clarify_request`` queueing. Offload to
    # the default thread pool to keep the loop responsive.
    return await asyncio.to_thread(_do_read_file, args["path"])


def _do_write_file(path_arg: str, content: str) -> str:
    from tool.tool_file_ops import resolve_workspace_path

    path = resolve_workspace_path(path_arg, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(content, encoding="utf-8")
    # Return a TERSE confirmation — deliberately NOT echoing ``content`` (nor a
    # structuredPatch). The full {type, filePath, content} blob is so
    # answer-shaped that smaller models paste it back verbatim as their final
    # reply, and the user sees raw JSON instead of "saved to <file>". Same
    # reasoning as the ``memory`` tool's one-token "ok" return. The path + byte
    # count are enough for the model to write a natural confirmation.
    return json.dumps(
        {
            "ok": True,
            "action": "update" if original is not None else "create",
            "path": str(path),
            "bytes": len(content.encode("utf-8")),
        },
        ensure_ascii=False,
    )


async def _wrap_write_file(args: dict) -> Any:
    return await asyncio.to_thread(_do_write_file, args["path"], args["content"])


async def _wrap_list_directory(args: dict) -> Any:
    from tool.tool_file_ops import list_directory_structured

    # ``list_directory_structured`` runs the same workspace-boundary check
    # under the hood. The previous "absolute path → serve directly" escape
    # let a prompt-injected ``list_directory path="C:\\Users"`` enumerate the
    # user's home directory; closing that loophole brings this wrapper in
    # line with the read/write/edit wrappers and the README's promise.
    return await asyncio.to_thread(list_directory_structured, args.get("path", "."))


async def _wrap_grep_search(args: dict) -> Any:
    from tool.tool_file_ops import grep_search_files

    # Recursive grep walks the workspace tree synchronously and can take
    # multiple seconds on a large repo — off-load so the event loop keeps
    # pumping SSE events while ripgrep runs.
    return await asyncio.to_thread(
        grep_search_files,
        pattern=args["pattern"],
        path=args.get("path", "."),
        glob_pattern=args.get("glob_pattern"),
        output_mode=args.get("output_mode", "files_with_matches"),
        context=args.get("context", 0),
        line_numbers=args.get("line_numbers", True),
        case_insensitive=args.get("case_insensitive", False),
        head_limit=args.get("head_limit", 250),
        offset=args.get("offset", 0),
        multiline=args.get("multiline", False),
    )


async def _wrap_glob_search(args: dict) -> Any:
    from tool.tool_file_ops import glob_search_files

    return await asyncio.to_thread(
        glob_search_files,
        pattern=args["pattern"],
        path=args.get("path", "."),
    )


# Diagnostic log for run_python calls. Capped + rotated so a long-running
# session can't fill the disk — the file is purely for "is the call hung?"
# investigation, not an audit trail.
_RUNPYTHON_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB; matches telemetry's cap


def _rotate_runpython_log(log_path) -> None:
    """Move runpython.log → runpython.log.1 if oversized (one backup only).

    Best-effort: any OSError (Windows file-in-use, permission denied) is
    silently swallowed so the log overhead never fails the actual run_python
    call the user is waiting on.
    """
    try:
        if log_path.exists() and log_path.stat().st_size > _RUNPYTHON_LOG_MAX_BYTES:
            rotated = log_path.with_suffix(".log.1")
            if rotated.exists():
                rotated.unlink()
            log_path.rename(rotated)
    except OSError:
        pass


def _do_run_python(code: str, timeout: int) -> str:
    import time as _time
    from pathlib import Path as _Path
    from tool.tool_shell import run_python_code

    log_path = _Path(".agent/runtime/tool-agent-runpython.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_runpython_log(log_path)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"\n=== ENTER {_time.strftime('%Y-%m-%d %H:%M:%S')} timeout={timeout}s ===\n"
            f"--- code ---\n{code}\n"
        )
    t0 = _time.monotonic()
    result = run_python_code(code=code, timeout=timeout)
    elapsed = _time.monotonic() - t0
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"--- EXIT elapsed={elapsed:.1f}s ---\n{result}\n")
    return result


async def _wrap_run_python(args: dict) -> Any:
    from tool.tool_shell import DEFAULT_SUBPROCESS_TIMEOUT

    code = args["code"]
    timeout = int(args.get("timeout", DEFAULT_SUBPROCESS_TIMEOUT))
    # ``run_python_code`` uses ``subprocess.communicate`` (blocking) and the
    # default timeout is 180s. Running it directly from this async function
    # froze the asyncio loop for the full duration — orchestrator's SSE
    # heartbeat stalled, ``clarify_request`` events queued behind the call,
    # and the user saw the spinner go silent. ``asyncio.to_thread`` keeps
    # the loop pumping. Default 180s matches ``run_command`` so reading
    # binary formats (.docx via python-docx, .pdf via pypdf) survives lxml's
    # cold-start cost.
    return await asyncio.to_thread(_do_run_python, code, timeout)


async def _wrap_run_command(args: dict) -> Any:
    from tool.tool_shell import DEFAULT_SUBPROCESS_TIMEOUT, run_shell_command

    # Default ``DEFAULT_SUBPROCESS_TIMEOUT`` (180s) so ``pip install <pkg>``
    # actually completes on slow networks. The constant is defined in
    # ``tool/tool_shell`` so the @tool surface (``tool/tools.py``) and this
    # wrapper agree by construction. ``to_thread`` per the rationale on
    # ``_wrap_run_python``.
    return await asyncio.to_thread(
        run_shell_command,
        args["command"],
        int(args.get("timeout", DEFAULT_SUBPROCESS_TIMEOUT)),
    )


def _do_web_search(query: str, limit: int, provider: str) -> str:
    from tool.tool_web import web_search
    return json.dumps(
        web_search(query=query, limit=limit, provider=provider),
        ensure_ascii=False, indent=2,
    )


async def _wrap_web_search(args: dict) -> Any:
    # ``urllib`` HTTP fetch is fully synchronous and the default timeout is
    # 30s — running it from this async function blocks every other coroutine
    # in the tool-agent process. Offload.
    return await asyncio.to_thread(
        _do_web_search,
        args["query"],
        int(args.get("limit", 5)),
        args.get("provider", "auto"),
    )


def _do_web_extract(url: str, max_chars: int) -> str:
    from tool.tool_web import web_extract
    return json.dumps(
        web_extract(url=url, max_chars=max_chars),
        ensure_ascii=False, indent=2,
    )


async def _wrap_web_extract(args: dict) -> Any:
    return await asyncio.to_thread(
        _do_web_extract,
        args["url"],
        int(args.get("max_chars", 8000)),
    )


def _do_web_crawl(url, max_pages, max_chars_per_page, same_host_only, include_links):
    from tool.tool_web import web_crawl
    return json.dumps(
        web_crawl(
            url=url,
            max_pages=max_pages,
            max_chars_per_page=max_chars_per_page,
            same_host_only=same_host_only,
            include_links=include_links,
        ),
        ensure_ascii=False, indent=2,
    )


async def _wrap_web_crawl(args: dict) -> Any:
    return await asyncio.to_thread(
        _do_web_crawl,
        args["url"],
        int(args.get("max_pages", 5)),
        int(args.get("max_chars_per_page", 4000)),
        bool(args.get("same_host_only", True)),
        bool(args.get("include_links", False)),
    )


async def _wrap_memory(args: dict) -> Any:
    """Persist a fact across turns / sessions.

    Wraps :func:`tool.tool_memory.memory` with a deliberately *un*-quotable
    return shape. The original tool returns a structured dict with success /
    entries / usage fields -- LLMs see something that pretty and just paste
    it as the final answer (so the user gets ``{success: true, entries:
    [...]}`` instead of a natural-language reply).

    By returning a terse instruction string here, there's nothing tempting
    to copy and the model is forced to write a real conversational reply.
    """
    import json as _json

    from tool import tool_memory

    action = str(args.get("action") or "").strip()
    target = str(args.get("target") or "memory").strip() or "memory"
    content = str(args.get("content") or "")
    old_text = str(args.get("old_text") or "")
    result = tool_memory.memory(
        action=action, target=target, content=content, old_text=old_text,
    )
    if not result.get("success"):
        # Surface the actual error so the model can recover (e.g. "entry
        # already exists" -> use replace instead).
        return _json.dumps(result, ensure_ascii=False)

    if action == "read":
        # Reads ARE supposed to return content; the model needs to see it.
        entries = result.get("entries") or []
        if not entries:
            return f"(no entries in {target})"
        return "\n".join(f"- {e}" for e in entries)

    # add / replace / remove: return the minimum the agent loop can possibly
    # mistake for a final answer. Anything longer or more sentence-like and
    # smaller models (DeepSeek's flash variants especially) just copy the
    # tool message verbatim as their reply. A single token leaves them with
    # no choice but to look back at the user's original message and write
    # something real.
    return "ok"


async def _wrap_clarify(args: dict) -> Any:
    """Bridge the ``clarify`` tool to the orchestrator via the per-turn
    event queue set up in ``agents.tool_agent.main``.

    Returns a structured "unavailable" string when called outside an SSE
    streaming dispatch (e.g. someone hit ``clarify`` over the synchronous
    MCP path) instead of hanging. See ``clarify_bridge.request``.
    """
    from agents.tool_agent import clarify_bridge

    question = str(args.get("question") or "").strip()
    if not question:
        return json.dumps(
            {"error": "clarify requires a non-empty question."},
            ensure_ascii=False,
        )
    choices = args.get("choices")
    if choices is not None and not isinstance(choices, list):
        choices = None
    answer = await clarify_bridge.request(question, choices)
    return json.dumps(
        {"question": question, "choices_offered": choices, "user_response": answer},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Tool map
# ---------------------------------------------------------------------------

_TOOL_MAP: dict[str, tuple] = {
    "read_file": (
        _wrap_read_file,
        {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Absolute or workspace-relative path to the file."},
            },
        },
        "Read a file and return its contents as JSON.",
    ),
    "write_file": (
        _wrap_write_file,
        {
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string", "description": "Path to write."},
                "content": {"type": "string", "description": "UTF-8 text to write."},
            },
        },
        "Write (create or overwrite) a file with the given content.",
    ),
    "list_directory": (
        _wrap_list_directory,
        {
            "type": "object",
            "required": [],
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: workspace root)."},
            },
        },
        "List the contents of a directory.",
    ),
    "grep_search": (
        _wrap_grep_search,
        {
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for."},
                "path": {"type": "string", "description": "Directory or file to search (default: workspace root)."},
                "glob_pattern": {"type": "string", "description": "Optional glob filter, e.g. '*.py'."},
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "description": "Output style.",
                },
                "context": {"type": "integer", "description": "Lines of context around each match."},
                "line_numbers": {"type": "boolean", "description": "Include line numbers in content output."},
                "case_insensitive": {"type": "boolean"},
                "head_limit": {"type": "integer"},
                "offset": {"type": "integer"},
                "multiline": {"type": "boolean"},
            },
        },
        "Search files for a regex pattern using ripgrep-style semantics.",
    ),
    "glob_search": (
        _wrap_glob_search,
        {
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'."},
                "path": {"type": "string", "description": "Base directory (default: workspace root)."},
            },
        },
        "Find files matching a glob pattern.",
    ),
    "run_python": (
        _wrap_run_python,
        {
            "type": "object",
            "required": ["code"],
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Python source to execute via `python -c`. Use this when "
                        "the built-in file tools cannot handle the format — for "
                        "example, reading .docx with python-docx, .pdf with "
                        "pypdf, or .xlsx with openpyxl. Print results to stdout."
                    ),
                },
                "timeout": {"type": "integer", "description": "Seconds before the subprocess is killed (default 180)."},
            },
        },
        "Execute Python code in a subprocess; returns JSON with stdout/stderr/exitCode.",
    ),
    "run_command": (
        _wrap_run_command,
        {
            "type": "object",
            "required": ["command"],
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to execute. Use this as a fallback when "
                        "the built-in file tools cannot complete the task — e.g. "
                        "running CLI utilities, inspecting binary file headers, "
                        "or piping with grep/awk."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Seconds before the subprocess is killed. Default 180. "
                        "Bump for pip installs over slow networks."
                    ),
                },
            },
        },
        "Execute a shell command; returns JSON with stdout/stderr/exitCode.",
    ),
    "web_search": (
        _wrap_web_search,
        {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "limit": {"type": "integer", "description": "Max results (1-10, default 5)."},
                "provider": {
                    "type": "string",
                    "enum": ["auto", "duckduckgo", "tavily"],
                    "description": "auto picks Tavily if TAVILY_API_KEY is set, else DuckDuckGo.",
                },
            },
        },
        "Search the web; returns JSON {provider, query, results:[{title,url,snippet}]}.",
    ),
    "web_extract": (
        _wrap_web_extract,
        {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "description": "HTTP(S) URL to fetch."},
                "max_chars": {"type": "integer", "description": "Truncate text to this many chars (default 8000)."},
            },
        },
        "Fetch a URL and return readable text; no JS rendering. Use this for "
        "any 'what does this page say' / 'summarize this URL' request.",
    ),
    "web_crawl": (
        _wrap_web_crawl,
        {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "description": "Seed URL to BFS-crawl."},
                "max_pages": {"type": "integer", "description": "Page cap (default 5, hard cap 25)."},
                "max_chars_per_page": {"type": "integer", "description": "Per-page text cap (default 4000)."},
                "same_host_only": {"type": "boolean", "description": "Restrict to seed host (default true)."},
                "include_links": {"type": "boolean", "description": "Include extracted hrefs per page."},
            },
        },
        "BFS-crawl a small set of pages from a seed URL. Use when one page "
        "isn't enough; prefer web_extract when one page is.",
    ),
    "memory": (
        _wrap_memory,
        {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "replace", "remove", "read"],
                    "description": (
                        "add: append a new entry. replace: overwrite an existing "
                        "entry (use old_text to identify which). remove: delete an "
                        "entry (use old_text or content as the substring match). "
                        "read: return current entries."
                    ),
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": (
                        "memory = agent's own notes (conventions, project facts). "
                        "user = the user's profile (name, preferences, etc.). "
                        "Default: memory."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "For add: the new entry text. For replace: the new entry "
                        "text replacing the matched old entry. For remove: a "
                        "substring identifying the entry to delete (alternative "
                        "to old_text)."
                    ),
                },
                "old_text": {
                    "type": "string",
                    "description": (
                        "Substring of an existing entry to identify it. Used by "
                        "replace and remove."
                    ),
                },
            },
        },
        "Persist a fact across turns/sessions. Use this when the user says "
        "'remember that ...', states a name/preference, or asks you to forget "
        "or correct something. Two stores: 'user' for facts about the user "
        "(name, preferences); 'memory' for project/agent notes. Entries are "
        "auto-injected into future system prompts so you don't have to query "
        "them — call this only to write/update/delete.",
    ),
    "clarify": (
        _wrap_clarify,
        {
            "type": "object",
            "required": ["question"],
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Question to put to the user.",
                },
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 4,
                    "description": "Optional multiple-choice options (up to 4).",
                },
            },
        },
        "Ask the user a clarifying question. Use when the request is ambiguous "
        "or has meaningful trade-offs the model cannot resolve. Returns the "
        "user's answer as JSON.",
    ),
}


# Tools NOT registered as MCP capabilities on the orchestrator. The intent:
# the planner cannot pick `run_python` / `run_command` / `clarify` as
# top-level dispatch capabilities — those would short-circuit the ReAct
# loop's autonomy (run_*) or hit the synchronous MCP path with no UI
# callback channel (clarify) and just hang.
#
# They remain reachable in two other ways, and that is by design:
#
# 1. tool-agent's own ReAct loop (``make_langchain_tools``) — the agent
#    decides when to shell out OR when to ask the user, which is exactly
#    the autonomy we want.
# 2. skill-agent → A2A → tool-agent: ``_call_remote_tool`` mints a
#    JWT-scoped grant via ``_mint_tool_grant``. Whether that grant is
#    actually allowed is gated by ``_SKILL_INNER_WHITELIST[mode]`` — under
#    ``workspace-write`` and ``danger-full-access`` that whitelist is ``*``,
#    so vetted skills can run their domain scripts. Under ``read-only`` the
#    outer ``_MODE_WHITELIST`` blocks skill dispatch upstream, so the inner
#    whitelist never sees a request.
#
# Adding a tool here only removes the *direct planner dispatch* path, not
# the *internal* paths. Document the actual blast radius before adding.
_INTERNAL_ONLY: frozenset[str] = frozenset(
    {"run_python", "run_command", "clarify", "memory"}
)
# ``memory`` is intentionally NOT a planner-dispatchable capability: when the
# orchestrator picks ``capability=memory`` directly, the dispatch goes through
# the MCP path and the tool's terse ``"ok"`` return shows up to the user as
# the final answer. The right flow is planner -> tool.task -> tool-agent's
# ReAct loop -> memory tool, where the ReAct loop is responsible for following
# up with a real natural-language reply. tool-agent's own ``make_langchain_tools``
# (above) does NOT consult ``_INTERNAL_ONLY``, so the ReAct loop still gets
# ``memory`` bound for autonomous use.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_tool_specs() -> list[ToolSpec]:
    """Return ToolSpec objects for tools the orchestrator may dispatch via MCP."""
    return [
        ToolSpec(name=name, description=desc, input_schema=schema, handler=handler)
        for name, (handler, schema, desc) in _TOOL_MAP.items()
        if name not in _INTERNAL_ONLY
    ]


def _make_tool_coroutine(handler, name: str):
    """Create an async callable that forwards keyword args to the dict-based handler."""

    async def _tool_coroutine(**kwargs: Any) -> Any:
        return await handler(kwargs)

    _tool_coroutine.__name__ = name
    return _tool_coroutine


def tools_for_mode(mode: str) -> list[str]:
    """Names of the tools tool-agent's ReAct loop may invoke under *mode*.

    Consulted by ``make_langchain_tools`` so a read-only delegation reaches
    tool-agent with ``write_file`` / ``run_command`` simply not bound to the
    model. Returns the full ``_TOOL_MAP`` key list when the mode whitelist is
    ``["*"]`` (danger-full-access).
    """
    from agents.shared.permission_modes import _TOOL_AGENT_MODE_TOOLS

    allowed = _TOOL_AGENT_MODE_TOOLS.get(mode, [])
    if "*" in allowed:
        return list(_TOOL_MAP.keys())
    # Preserve _TOOL_MAP order so the prompt's tool listing stays stable.
    return [n for n in _TOOL_MAP.keys() if n in set(allowed)]


def make_langchain_tools(mode: str = "danger-full-access") -> list:
    """Return LangChain-compatible StructuredTool objects for the ReAct loop.

    *mode* gates which tools are bound. The previous unconditional binding
    let an orchestrator-delegated ``tool.task`` reach ``run_command`` even
    when the user had selected ``read-only`` — bypassing the permission gate
    entirely (the gate only protected the legacy direct-MCP dispatch path).
    The fix lives here rather than in the gate itself because it's cleaner
    to never tell the model the tool exists than to refuse the call after.

    Default is ``danger-full-access`` so in-process callers (unit tests, the
    legacy single-agent loop) keep working without a mode threaded through.
    """
    from langchain_core.tools import StructuredTool

    allowed = set(tools_for_mode(mode))
    result: list = []
    for name, (handler, schema, desc) in _TOOL_MAP.items():
        if name not in allowed:
            continue
        coro = _make_tool_coroutine(handler, name)
        tool = StructuredTool(
            name=name,
            description=desc,
            args_schema=schema,
            coroutine=coro,
        )
        result.append(tool)
    return result


async def execute_tool(name: str, args: dict) -> Any:
    """Dispatch ``args`` to the tool named ``name``.

    Raises ``ValueError`` if the tool is not registered.
    Raises ``AuthzError`` if the JWT grant is missing, expired, or does not
    list ``name`` in its ``allowed_tools`` claim.
    """
    entry = _TOOL_MAP.get(name)
    if entry is None:
        raise ValueError(f"unknown tool: {name}")
    handler, _schema, _desc = entry

    # Extract and verify the authz grant from _meta.
    meta = args.get("_meta") or {}
    grant = meta.get("authz_grant")
    if grant is None:
        raise AuthzError("missing authz_grant in _meta")
    verify_grant(grant, key=_hmac_key(), requested_tool=name)

    # Strip _meta before forwarding to the underlying tool.
    real_args = {k: v for k, v in args.items() if k != "_meta"}
    return await handler(real_args)
