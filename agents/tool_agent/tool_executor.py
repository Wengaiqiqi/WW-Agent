"""Bridge between existing in-process tool registry and MCP ToolSpec API.

Reuses tool/*.py functions where their signatures are trivially wrappable.
This module only adapts signatures and produces JSON schemas for MCP
``tools/list``.

The workspace-path restriction enforced by ``tool/tool_file_ops.py`` is
intentionally bypassed here: the tool-agent runs as a separate MCP process
whose access control is governed by the MCP protocol layer, not the CLI's
workspace sandbox.
"""
from __future__ import annotations

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


async def _wrap_read_file(args: dict) -> Any:
    path = Path(args["path"])
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    return json.dumps(
        {
            "type": "text",
            "file": {
                "filePath": str(path.resolve()),
                "content": content,
                "numLines": len(lines),
                "startLine": 1,
                "totalLines": len(lines),
            },
        },
        ensure_ascii=False,
        indent=2,
    )


async def _wrap_write_file(args: dict) -> Any:
    path = Path(args["path"])
    content = args["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(content, encoding="utf-8")
    return json.dumps(
        {
            "type": "update" if original is not None else "create",
            "filePath": str(path.resolve()),
            "content": content,
        },
        ensure_ascii=False,
        indent=2,
    )


async def _wrap_list_directory(args: dict) -> Any:
    from tool.tool_file_ops import list_directory_structured, workspace_root
    import os

    path = args.get("path", ".")
    # If the path is absolute and outside the workspace, serve it directly.
    p = Path(path)
    if p.is_absolute():
        dirs = []
        files = []
        for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry), "type": "directory"})
            else:
                files.append({"name": entry.name, "path": str(entry), "type": "file", "size": entry.stat().st_size})
        return json.dumps(
            {"directory": str(p), "count": len(dirs) + len(files), "directories": dirs, "files": files},
            ensure_ascii=False,
            indent=2,
        )
    return list_directory_structured(path)


async def _wrap_grep_search(args: dict) -> Any:
    from tool.tool_file_ops import grep_search_files

    return grep_search_files(
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

    return glob_search_files(
        pattern=args["pattern"],
        path=args.get("path", "."),
    )


async def _wrap_run_python(args: dict) -> Any:
    import time as _time
    from pathlib import Path as _Path
    from tool.tool_shell import DEFAULT_SUBPROCESS_TIMEOUT, run_python_code

    code = args["code"]
    timeout = int(args.get("timeout", DEFAULT_SUBPROCESS_TIMEOUT))
    log_path = _Path(".agent/runtime/tool-agent-runpython.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Log ENTRY immediately so we can tell "never called" from "called but hung".
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"\n=== ENTER {_time.strftime('%Y-%m-%d %H:%M:%S')} timeout={timeout}s ===\n"
            f"--- code ---\n{code}\n"
        )
    t0 = _time.monotonic()
    # Default 180s to match run_command — reading .docx/.pdf/.xlsx via
    # python-docx / pypdf / openpyxl easily exceeds 30s on cold start
    # because lxml and friends are loaded at import time.
    result = run_python_code(code=code, timeout=timeout)
    elapsed = _time.monotonic() - t0
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"--- EXIT elapsed={elapsed:.1f}s ---\n{result}\n"
        )
    return result


async def _wrap_run_command(args: dict) -> Any:
    from tool.tool_shell import DEFAULT_SUBPROCESS_TIMEOUT, run_shell_command

    # Default ``DEFAULT_SUBPROCESS_TIMEOUT`` (180s) so `pip install <pkg>`
    # actually completes on slow networks. The constant is defined in
    # ``tool/tool_shell`` so the @tool surface (``tool/tools.py``) and this
    # wrapper agree by construction instead of by drift-prone duplication.
    return run_shell_command(
        command=args["command"],
        timeout=int(args.get("timeout", DEFAULT_SUBPROCESS_TIMEOUT)),
    )


async def _wrap_web_search(args: dict) -> Any:
    from tool.tool_web import web_search

    return json.dumps(
        web_search(
            query=args["query"],
            limit=int(args.get("limit", 5)),
            provider=args.get("provider", "auto"),
        ),
        ensure_ascii=False,
        indent=2,
    )


async def _wrap_web_extract(args: dict) -> Any:
    from tool.tool_web import web_extract

    return json.dumps(
        web_extract(
            url=args["url"],
            max_chars=int(args.get("max_chars", 8000)),
        ),
        ensure_ascii=False,
        indent=2,
    )


async def _wrap_web_crawl(args: dict) -> Any:
    from tool.tool_web import web_crawl

    return json.dumps(
        web_crawl(
            url=args["url"],
            max_pages=int(args.get("max_pages", 5)),
            max_chars_per_page=int(args.get("max_chars_per_page", 4000)),
            same_host_only=bool(args.get("same_host_only", True)),
            include_links=bool(args.get("include_links", False)),
        ),
        ensure_ascii=False,
        indent=2,
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
}


# Tools NOT registered as MCP capabilities on the orchestrator. The intent:
# the planner cannot pick `run_python` / `run_command` as a top-level dispatch
# capability (those would short-circuit the ReAct loop and dodge the cost-of-
# reflection that motivates the multi-agent split in the first place).
#
# They remain reachable in two other ways, and that is by design:
#
# 1. tool-agent's own ReAct loop (``make_langchain_tools``) — the agent
#    decides when to shell out, which is exactly the autonomy we want.
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
_INTERNAL_ONLY: frozenset[str] = frozenset({"run_python", "run_command"})


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


def make_langchain_tools() -> list:
    """Return the 5 tool handlers as LangChain-compatible StructuredTool objects.

    Used by ToolAgentLoop's create_react_agent so tool-agent can call tools
    in-process (no MCP/A2A round-trip through itself).
    """
    from langchain_core.tools import StructuredTool

    result: list = []
    for name, (handler, schema, desc) in _TOOL_MAP.items():
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
