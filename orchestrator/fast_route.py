"""Local heuristic routing that skips the planner LLM round-trip.

The planner LLM is what hides behind the initial ``Loading...`` spinner: for an
ambiguous chat-vs-tool message it earns its keep, but for a message that
*obviously* needs files / URLs / commands / tests / repo review it is pure
latency — a full LLM round-trip just to emit
``{"capability": "tool.task", ...}``. ``fast_route`` recognizes those obvious
cases with cheap string matching and returns the dispatch decision directly.

Shared by ``orchestrator.repl_controller`` (interactive) and
``orchestrator.turns.TurnRunner`` (the one-shot ``cli.py prompt`` path) so both
entry points get the same speed-up and the same routing behavior.
"""
from __future__ import annotations

import os

# URL markers imply a web fetch — a READ, safe under any permission mode.
_FAST_TOOL_URL_MARKERS = ("http://", "https://")

# File-extension markers imply a file operation, which may be a write. They
# route under full access, but NOT on their own under read-only (see the
# read-only marker selection in ``fast_route``).
_FAST_TOOL_FILE_MARKERS = (
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg",
    ".ini", ".csv", ".tsv", ".docx", ".pdf", ".xlsx", ".html", ".css",
    ".js", ".ts", ".tsx", ".vue", ".svg", ".png", ".jpg", ".jpeg",
)

_FAST_TOOL_MARKERS = _FAST_TOOL_URL_MARKERS + _FAST_TOOL_FILE_MARKERS

_FAST_TOOL_PREFIXES = (
    "read ", "open ", "inspect ", "list ", "search ", "grep ", "find ",
    "write ", "edit ", "modify ", "update ", "fix ", "delete ", "create ",
    "run ", "execute ", "debug ", "review ", "audit ", "analyze ",
    "optimise ", "optimize ", "profile ", "summarize ",
    "pytest", "python ", "git ", "npm ", "pip ", "dir ", "ls ",
)
# "test " removed: "test 一下这个想法" is plain chat, not a tool task.
# Specific testing verbs ("pytest", "run ") still match.

_FAST_TOOL_WORDS = (
    "read", "inspect", "search", "grep", "write", "edit", "modify",
    "update", "fix", "debug", "review", "audit", "analyze", "optimise",
    "optimize", "profile",
)

_FAST_TOOL_CJK = (
    "查看", "读取", "打开", "列出", "搜索", "查找", "修改", "更改",
    "更新", "修复", "删除", "创建", "写入", "保存", "运行", "执行",
    "调试", "审查", "检查", "分析", "优化", "总结",
)

# Read-only-safe subset of the CJK / prefix vocabulary. Used under read-only
# mode: a user-typed "查看 README" should still skip the planner LLM
# round-trip even when mutation isn't allowed; "保存到 a.txt" should not — it
# can't succeed anyway, and forcing it through the planner lets the LLM render
# a clean refusal in prose.
_FAST_TOOL_READONLY_PREFIXES = (
    "read ", "open ", "inspect ", "list ", "search ", "grep ", "find ",
    "review ", "audit ", "analyze ", "summarize ",
    "dir ", "ls ",
)
_FAST_TOOL_READONLY_WORDS = (
    "read", "inspect", "search", "grep", "review", "audit", "analyze",
)
_FAST_TOOL_READONLY_CJK = (
    "查看", "读取", "打开", "列出", "搜索", "查找",
    "审查", "检查", "分析", "总结",
)

# Referring expressions that indicate the user is talking about
# orchestrator-side context the tool-agent does NOT have. When detected,
# defer to the planner so it can resolve the referent inline or hand a richer
# task description down. (Pure-tool detection still wins when the message is
# *only* a path/URL.)
_REFERRING_TOKENS = (
    "上面", "下面", "刚才", "之前", "上一", "前面",
    "the above", "the previous", "what you just",
)


def fast_route(
    text: str,
    *,
    capabilities,
    mode: str = "danger-full-access",
) -> dict | None:
    """Return a local ``tool.task`` decision for obvious tool-agent work, else None.

    ``capabilities`` is the router's known capability list (used to defer
    ``CAP:arg`` style direct dispatch to the planner). ``mode`` is the active
    permission mode — under read-only only read-class verbs fast-route.

    Returns ``{"capability": "tool.task", "arguments": {"task": <text>}}`` to
    skip the planner, or ``None`` to fall through to the planner LLM.
    """
    if os.environ.get("LANGCHAIN_AGENT_DISABLE_FAST_ROUTE") == "1":
        return None

    stripped = text.strip()
    if not stripped:
        return None
    if ":" in stripped:
        cap, _, _arg = stripped.partition(":")
        if cap.strip() in set(capabilities):
            return None

    lower = stripped.lower()

    # Defer to the planner when the message refers to earlier conversation —
    # only the planner sees session history and can rewrite the referent.
    if any(tok in lower or tok in stripped for tok in _REFERRING_TOKENS):
        return None

    if mode == "read-only":
        prefixes = _FAST_TOOL_READONLY_PREFIXES
        words = _FAST_TOOL_READONLY_WORDS
        cjk = _FAST_TOOL_READONLY_CJK
        # Only URL markers route on their own under read-only. A bare file
        # mention ("保存到 a.txt", "write a.py") is ambiguous/possibly a write
        # and must defer to the planner (which renders a clean prose refusal)
        # UNLESS it also carries a read verb — in which case the read-only
        # prefix/word/CJK checks below already route it ("read config.py").
        markers = _FAST_TOOL_URL_MARKERS
    else:
        prefixes = _FAST_TOOL_PREFIXES
        words = _FAST_TOOL_WORDS
        cjk = _FAST_TOOL_CJK
        markers = _FAST_TOOL_MARKERS

    # NOTE: deliberately no bare ``lower.startswith(word)`` check here. It used
    # to misfire on ordinary prose that merely *begins* with a tool verb's
    # letters — "updates are great" (startswith "update"), "fixated on X"
    # (startswith "fix"), "reading is fun" (startswith "read") — and wrongly
    # delegated chat to the tool-agent. The imperative forms are already covered
    # by the trailing-space prefixes ("fix ", "update ", "read ") and by the
    # whole-word ``split()`` check below, both of which require a word boundary.
    should_delegate = (
        any(marker in lower for marker in markers)
        or any(lower.startswith(prefix) for prefix in prefixes)
        or any(word in lower.split() for word in words)
        or any(word in stripped for word in cjk)
    )
    if not should_delegate:
        return None
    return {
        "capability": "tool.task",
        "arguments": {"task": stripped},
    }
