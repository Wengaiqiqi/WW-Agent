from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
import json

from langchain_core.tools import tool

from tool.tool_file_ops import (
    edit_text_file,
    glob_search_files,
    grep_search_files,
    list_directory_structured,
    read_text_file,
    write_text_file,
)
from tool.tool_clarify import clarify as clarify_dispatch
from tool.tool_memory import memory_tool as memory_dispatch
from tool.tool_patch import apply_patch_tool
from tool.tool_web import (
    web_crawl as web_crawl_impl,
    web_extract as web_extract_impl,
    web_search as web_search_impl,
)
from tool.tool_permissions import authorize_tool
from tool.tool_registry import TOOL_SPECS, required_permission_for, tool_manifest_text
from tool.tool_shell import (
    DEFAULT_SUBPROCESS_TIMEOUT,
    run_python_code,
    run_shell_command,
)
from tool.tool_osv import osv_lookup
from tool.tool_homeassistant import dispatch as ha_dispatch
from tool.tool_x_search import x_search as x_search_impl
from tool.tool_vision import vision_analyze as vision_analyze_impl
from tool.tool_moa import mixture_of_agents as moa_impl

import agent_paths


def _authorize(name: str, payload: str = "") -> str | None:
    try:
        authorize_tool(name, required_permission_for(name), payload)
        return None
    except PermissionError as exc:
        return f"Permission denied: {exc}"


@tool
def calculator(expression: str) -> str:
    """Evaluate a mathematical expression. Supports arithmetic, powers, and common math functions.

    Examples: "2 + 3 * 4", "sqrt(144)", "sin(pi / 2)".
    """
    if denied := _authorize("calculator", expression):
        return denied
    try:
        import ast
        import operator

        ops = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Pow: operator.pow,
            ast.USub: operator.neg,
            ast.UAdd: operator.pos,
        }

        funcs = {
            "abs": abs,
            "round": round,
            "sin": math.sin,
            "cos": math.cos,
            "tan": math.tan,
            "sqrt": math.sqrt,
            "log": math.log,
            "log10": math.log10,
            "pow": pow,
        }

        consts = {
            "pi": math.pi,
            "e": math.e,
        }

        def safe_eval(node):
            if isinstance(node, ast.Constant):
                return node.value
            elif isinstance(node, ast.BinOp):
                left = safe_eval(node.left)
                right = safe_eval(node.right)
                op = ops.get(type(node.op))
                if op is None:
                    raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
                return op(left, right)
            elif isinstance(node, ast.UnaryOp):
                operand = safe_eval(node.operand)
                op = ops.get(type(node.op))
                if op is None:
                    raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
                return op(operand)
            elif isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name):
                    raise ValueError("Only simple function calls are allowed")
                func_name = node.func.id
                if func_name not in funcs:
                    raise ValueError(f"Unknown function: {func_name}")
                args = [safe_eval(arg) for arg in node.args]
                return funcs[func_name](*args)
            elif isinstance(node, ast.Name):
                if node.id not in consts:
                    raise ValueError(f"Unknown constant: {node.id}")
                return consts[node.id]
            else:
                raise ValueError(f"Unsupported expression type: {type(node).__name__}")

        tree = ast.parse(expression, mode='eval')
        result = safe_eval(tree.body)
        return str(result)
    except Exception as exc:
        return f"Calculation error: {exc}"


@tool
def current_datetime() -> str:
    """Return the current local date and time.

    Only use this when the user explicitly asks for the current date, time,
    timestamp, today, now, or another time-sensitive fact. Do not use it for
    greetings or identity/model questions.
    """
    if denied := _authorize("current_datetime"):
        return denied
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S (%A)")


@tool
def list_directory(path: str = ".") -> str:
    """List files and directories under a workspace path as structured JSON."""
    if denied := _authorize("list_directory", path):
        return denied
    try:
        return list_directory_structured(path)
    except Exception as exc:
        return f"Directory listing error: {exc}"


@tool
def read_file(path: str, offset: int = 0, limit: int | None = None) -> str:
    """Read a text file from the workspace as structured JSON.

    Args:
        path: File path relative to the workspace, or an absolute path inside it.
        offset: Zero-based line offset.
        limit: Optional maximum number of lines to return. Omit or pass null to read to the end of the file.
    """
    if denied := _authorize("read_file", path):
        return denied
    try:
        return read_text_file(path, offset=offset, limit=limit)
    except Exception as exc:
        return f"Read file error: {exc}"


@tool
def write_file(path: str, content: str) -> str:
    """Write a text file inside the workspace and return structured JSON with a patch summary."""
    if denied := _authorize("write_file", path):
        return denied
    try:
        return write_text_file(path, content)
    except Exception as exc:
        return f"Write file error: {exc}"


@tool
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace text in a workspace file and return structured JSON with a patch summary.

    Use this for targeted edits instead of rewriting a whole file.
    """
    if denied := _authorize("edit_file", path):
        return denied
    try:
        return edit_text_file(path, old_string, new_string, replace_all=replace_all)
    except Exception as exc:
        return f"Edit file error: {exc}"


@tool
def glob_search(pattern: str, path: str = ".") -> str:
    """Find files by glob pattern under a workspace path.

    Examples: pattern="*.py", pattern="**/*.md".
    """
    if denied := _authorize("glob_search", f"{path}/{pattern}"):
        return denied
    try:
        return glob_search_files(pattern, path=path)
    except Exception as exc:
        return f"Glob search error: {exc}"


@tool
def grep_search(
    pattern: str,
    path: str = ".",
    glob_pattern: str | None = None,
    output_mode: str = "content",
    context: int = 0,
    line_numbers: bool = True,
    case_insensitive: bool = False,
    head_limit: int = 250,
    offset: int = 0,
    multiline: bool = False,
) -> str:
    """Search workspace file contents with a regular expression.

    output_mode can be "content", "files_with_matches", or "count".
    """
    if denied := _authorize("grep_search", pattern):
        return denied
    try:
        return grep_search_files(
            pattern=pattern,
            path=path,
            glob_pattern=glob_pattern,
            output_mode=output_mode,
            context=context,
            line_numbers=line_numbers,
            case_insensitive=case_insensitive,
            head_limit=head_limit,
            offset=offset,
            multiline=multiline,
        )
    except Exception as exc:
        return f"Grep search error: {exc}"


@tool
def apply_patch(patch: str) -> str:
    """Apply a V4A unified-diff patch that may modify, add, delete, or move multiple files.

    Format:

        *** Begin Patch
        *** Update File: path/to/file.py
        @@ optional context hint @@
         context line
        -removed line
        +added line
        *** Add File: path/to/new.py
        +line 1
        +line 2
        *** Delete File: path/to/old.py
        *** Move File: old.py -> new.py
        *** End Patch

    Validates all operations first; if any hunk fails to match, NO files are
    written. Use this for multi-file edits or when several changes share a
    review boundary; prefer ``edit_file`` for single-spot replacements.
    """
    if denied := _authorize("apply_patch", patch[:80]):
        return denied
    try:
        return apply_patch_tool(patch)
    except Exception as exc:
        return f"Apply patch error: {exc}"


@tool
def run_python(code: str, timeout: int = DEFAULT_SUBPROCESS_TIMEOUT) -> str:
    """Execute Python code in a subprocess and return structured JSON with stdout, stderr, and exit code.

    ``timeout`` defaults to ``DEFAULT_SUBPROCESS_TIMEOUT`` (180s) so pip
    installs, large-document parsers, and other slow-cold-start scripts
    succeed without the caller having to remember to raise the limit.
    """
    if denied := _authorize("run_python", "python code"):
        return denied
    try:
        return run_python_code(code, timeout=timeout)
    except Exception as exc:
        return f"Python execution error: {exc}"


@tool
def run_command(command: str, timeout: int = DEFAULT_SUBPROCESS_TIMEOUT) -> str:
    """Execute a shell command in the workspace and return structured JSON.

    This is a dangerous tool. Use it only when file/search/Python tools are
    insufficient. ``timeout`` defaults to ``DEFAULT_SUBPROCESS_TIMEOUT`` (180s).
    """
    if denied := _authorize("run_command", command):
        return denied
    try:
        return run_shell_command(command, timeout=timeout)
    except Exception as exc:
        return f"Command execution error: {exc}"


@tool
def web_search(query: str, limit: int = 5, provider: str = "auto") -> str:
    """Search the web and return JSON ``{provider, query, results: [{title, url, snippet}]}``.

    ``provider``: "auto" (Tavily when ``TAVILY_API_KEY`` is set, otherwise
    DuckDuckGo), "duckduckgo" (no key), or "tavily" (needs ``TAVILY_API_KEY``).
    Use this to discover URLs to ``web_extract``; do not rely on snippets alone
    for factual claims — fetch the page when accuracy matters.
    """
    if denied := _authorize("web_search", query[:80]):
        return denied
    try:
        return json.dumps(web_search_impl(query, limit=limit, provider=provider), ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Web search error: {exc}"


@tool
def web_extract(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return ``{title, url, text}`` JSON with HTML stripped.

    No JavaScript rendering — JS-heavy pages may return mostly chrome. The
    text is truncated to ``max_chars`` (default 8000); ``truncated: true``
    indicates more content was available.
    """
    if denied := _authorize("web_extract", url):
        return denied
    try:
        return json.dumps(web_extract_impl(url, max_chars=max_chars), ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Web extract error: {exc}"


@tool
def memory(action: str, target: str = "memory", content: str = "", old_text: str = "") -> str:
    """Curate cross-session memory in MEMORY.md (agent notes) or USER.md (user profile).

    Persistent memory survives REPL restarts. A frozen snapshot of both files
    is injected into the system prompt at session start, so what you write
    here today shapes how you behave tomorrow.

    Args:
        action: "add" | "replace" | "remove" | "read".
        target: "memory" (default; agent's notes about code, environment,
            conventions, prior failures) or "user" (preferences, goals, style).
        content: New entry text (for add) or replacement (for replace).
        old_text: Substring identifying which entry to replace / remove.

    Each file has a per-file char budget; when full, replace or remove
    obsolete entries before adding new ones. Do NOT store secrets, API keys,
    or anything that should not appear in the system prompt.
    """
    if denied := _authorize("memory", f"{action}/{target}"):
        return denied
    try:
        return memory_dispatch(action, target=target, content=content, old_text=old_text)
    except Exception as exc:
        return f"Memory error: {exc}"


@tool
def clarify(question: str, choices: list[str] | None = None) -> str:
    """Ask the user a clarifying question before continuing.

    Use this when the request is ambiguous, when you need the user to choose
    between meaningful trade-offs, or when you want post-task feedback. Do NOT
    use it as a generic safety-confirm for dangerous operations (the shell
    tool handles that).

    Modes:
    - **Multiple choice**: pass up to 4 options. The CLI presents arrow-key
      navigation and automatically appends an "Other (type your answer)" entry
      for free-form input.
    - **Open-ended**: omit ``choices`` to get a free-text response.
    """
    if denied := _authorize("clarify", question[:80]):
        return denied
    try:
        return clarify_dispatch(question, choices)
    except Exception as exc:
        return f"Clarify error: {exc}"


@tool
def sleep(duration_ms: int) -> str:
    """Wait for a specified duration in milliseconds."""
    if denied := _authorize("sleep", str(duration_ms)):
        return denied
    duration = max(0, duration_ms) / 1000
    time.sleep(duration)
    return f"Slept for {duration_ms}ms"


@tool
def todo_write(todos: list[dict[str, str]]) -> str:
    """Update the structured task list for the current session.

    Each todo should include content, activeForm, and status. Status should be
    pending, in_progress, or completed.
    """
    if denied := _authorize("todo_write"):
        return denied
    try:
        todos_file = agent_paths.todos_path()
        todos_file.parent.mkdir(parents=True, exist_ok=True)
        normalized = []
        for item in todos:
            status = item.get("status", "pending")
            if status not in {"pending", "in_progress", "completed"}:
                return f"Todo status must be pending, in_progress, or completed: {status}"
            content = item.get("content", "").strip()
            if not content:
                return "Todo content is required"
            normalized.append(
                {
                    "content": content,
                    "activeForm": item.get("activeForm", content),
                    "status": status,
                }
            )
        todos_file.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.dumps({"updated": len(normalized), "todos": normalized}, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Todo update error: {exc}"


@tool
def config(setting: str, value: str | bool | int | float | None = None) -> str:
    """Get or set local agent settings stored in the agent config directory."""
    if denied := _authorize("config", setting):
        return denied
    try:
        settings_file = agent_paths.settings_path()
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings = {}
        if settings_file.exists():
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
        if value is None:
            return json.dumps(
                {"setting": setting, "value": settings.get(setting), "exists": setting in settings},
                ensure_ascii=False,
                indent=2,
            )
        settings[setting] = value
        settings_file.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.dumps({"operation": "set", "setting": setting, "newValue": value}, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Config error: {exc}"


@tool
def tool_manifest() -> str:
    """Return the registered tool manifest with permission requirements."""
    if denied := _authorize("tool_manifest"):
        return denied
    return tool_manifest_text()


@tool
def web_crawl(
    url: str,
    max_pages: int = 5,
    max_chars_per_page: int = 4000,
    same_host_only: bool = True,
    include_links: bool = False,
) -> str:
    """BFS-crawl pages from ``url`` (same host by default).

    No JavaScript rendering and no LLM summarization — pages are HTML-stripped
    locally. ``max_pages`` is capped at 25. Useful for grabbing a small section
    of a doc site or scraping a blog index plus a few posts in one call.

    Do NOT use when a single page is enough — call ``web_extract`` instead.
    Do NOT use for JS-heavy SPAs (results will be mostly nav chrome).
    """
    if denied := _authorize("web_crawl", url):
        return denied
    try:
        return json.dumps(
            web_crawl_impl(
                url,
                max_pages=max_pages,
                max_chars_per_page=max_chars_per_page,
                same_host_only=same_host_only,
                include_links=include_links,
            ),
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return f"Web crawl error: {exc}"


@tool
def osv_check(
    package: str,
    ecosystem: str = "npm",
    version: str | None = None,
    malware_only: bool = False,
) -> str:
    """Query the public OSV API for advisories on a package.

    ``ecosystem`` is the OSV ecosystem name (``npm``, ``PyPI``, ``Go``,
    ``crates.io``, ``Maven``, ``RubyGems``, ``Packagist``, ``Hex``, etc.).
    Set ``malware_only=True`` to filter to confirmed MAL-* advisories (skip
    regular CVEs); useful before launching an MCP package via npx/uvx.

    Do NOT use as a general "is this package good" review — OSV only knows
    about reported vulnerabilities/malware, not code quality or licensing.
    Do NOT use without an ecosystem; defaults to npm and silently misses
    PyPI packages.
    """
    if denied := _authorize("osv_check", f"{ecosystem}/{package}"):
        return denied
    try:
        return json.dumps(
            osv_lookup(package, ecosystem, version=version, malware_only=malware_only),
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return f"OSV check error: {exc}"


@tool
def home_assistant(
    action: str,
    domain: str | None = None,
    area: str | None = None,
    entity_id: str | None = None,
    service: str | None = None,
    data: dict | str | None = None,
) -> str:
    """Control / inspect a Home Assistant instance over the REST API.

    Requires ``HASS_TOKEN`` (and optionally ``HASS_URL``) in the environment.

    Actions:
      - ``list_entities`` — optionally filter by ``domain`` and/or ``area``.
      - ``get_state``     — requires ``entity_id``.
      - ``list_services`` — optionally filter by ``domain``.
      - ``call_service``  — requires ``domain`` + ``service``; ``entity_id``
        and ``data`` (dict or JSON string) are optional. Domains that allow
        shell/code execution on the HA host are blocked.

    Do NOT call this when the user is asking generic smart-home questions
    that don't reference *their* devices — answer from general knowledge.
    Do NOT poll repeatedly; one ``get_state`` per logical question.
    """
    if denied := _authorize("home_assistant", action):
        return denied
    try:
        result = ha_dispatch(
            action,
            domain=domain,
            area=area,
            entity_id=entity_id,
            service=service,
            data=data,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Home Assistant error: {exc}"


@tool
def x_search(
    query: str,
    allowed_x_handles: list[str] | None = None,
    excluded_x_handles: list[str] | None = None,
    from_date: str = "",
    to_date: str = "",
    enable_image_understanding: bool = False,
    enable_video_understanding: bool = False,
) -> str:
    """Search X (Twitter) via xAI's hosted ``x_search`` Responses API tool.

    Requires ``XAI_API_KEY`` in the environment. Returns the model's answer
    plus citation links. Use for current discussion / reactions on X rather
    than general web pages. ``from_date`` / ``to_date`` are ``YYYY-MM-DD``.
    Up to 10 handles each in ``allowed_x_handles`` or ``excluded_x_handles``
    (the two are mutually exclusive).

    Do NOT use for general web search — prefer ``web_search`` for anything
    not specifically about X/Twitter content. Do NOT use to fetch a single
    known X post URL — use ``web_extract`` (Twitter often blocks scraping,
    but x_search is overkill for a known URL).
    """
    if denied := _authorize("x_search", query[:80]):
        return denied
    try:
        return json.dumps(
            x_search_impl(
                query=query,
                allowed_x_handles=allowed_x_handles,
                excluded_x_handles=excluded_x_handles,
                from_date=from_date,
                to_date=to_date,
                enable_image_understanding=enable_image_understanding,
                enable_video_understanding=enable_video_understanding,
            ),
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return f"x_search error: {exc}"


@tool
def vision_analyze(image: str, prompt: str = "Describe this image in detail.") -> str:
    """Analyze an image with a vision-capable LLM.

    ``image`` can be an ``http(s)://`` URL or a local file path. Uses your
    project's active LLM config (``config.build_llm``). Override the model
    via ``AGENT_VISION_MODEL`` if your default model is not vision-capable.

    Do NOT use on images that are clearly decorative or unrelated to the
    question (logos, dividers, icons). Do NOT use to "read" a screenshot
    of plain text — extract the text with run_python + tesseract or ask
    the user for the source text instead, since vision OCR is slower and
    less reliable.
    """
    if denied := _authorize("vision_analyze", image):
        return denied
    try:
        return json.dumps(
            vision_analyze_impl(image=image, prompt=prompt),
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return f"vision_analyze error: {exc}"


@tool
def mixture_of_agents(
    user_prompt: str,
    reference_models: list[str] | None = None,
    aggregator_model: str | None = None,
) -> str:
    """Run Mixture-of-Agents over multiple models, then synthesize a final answer.

    The reference models run in parallel; their responses are then fed to an
    aggregator model. All requests go through your active LLM config — so
    ``reference_models`` only changes the model name (base_url and api_key
    remain from the active config). Best for hard reasoning / coding tasks
    where multiple frontier models can disagree productively.

    Do NOT use for simple lookups, single-tool tasks, or anything you can
    answer directly — MoA spends 4-5× the latency and tokens of a single
    call. Do NOT use when your provider only supports one model name; the
    references would just sample the same model multiple times.
    """
    if denied := _authorize("mixture_of_agents", user_prompt[:80]):
        return denied
    try:
        return json.dumps(
            moa_impl(
                user_prompt=user_prompt,
                reference_models=reference_models,
                aggregator_model=aggregator_model,
            ),
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return f"mixture_of_agents error: {exc}"


ALL_TOOLS = [
    calculator,
    current_datetime,
    list_directory,
    read_file,
    write_file,
    edit_file,
    apply_patch,
    glob_search,
    grep_search,
    run_python,
    run_command,
    web_search,
    web_extract,
    web_crawl,
    memory,
    clarify,
    sleep,
    todo_write,
    config,
    tool_manifest,
    # Ported from hermes-agent
    osv_check,
    home_assistant,
    x_search,
    vision_analyze,
    mixture_of_agents,
]


def get_tool_specs():
    return TOOL_SPECS
