"""LLM-powered ReAct agent loop for tool-agent.

Uses LangGraph's create_react_agent to stream tool-calling iterations,
yielding typed event dicts the orchestrator's TUI consumes.
"""
from __future__ import annotations

import importlib
import json
import logging
import warnings
from typing import Any, AsyncIterator, Callable, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from agent_display import has_raw_tool_markup, is_langgraph_tool_chunk
from prompt_rules import (
    CONCISE_RULE,
    LANGUAGE_RULE,
    NO_RAW_TOOL_MARKUP_RULE,
    STOP_WHEN_DONE_RULE,
)

# Eagerly import create_react_agent at module load time. Doing it lazily inside
# _build_agent costs ~6 seconds of synchronous Python import on first call,
# which blocks the asyncio event loop and prevents uvicorn from flushing the
# first SSE chunk — so the orchestrator's spinner appears frozen at
# `Delegating to tool-agent...` for the entire import window.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="create_react_agent has been moved.*")
    from langgraph.prebuilt import create_react_agent  # noqa: E402

log = logging.getLogger(__name__)

# Libraries the LLM is likely to want for binary file formats. We probe at
# startup so the prompt can tell the model what's already installed (skip the
# `pip install` step entirely) and what isn't (then it must install).
_DOC_LIBS: dict[str, str] = {
    "docx": "python-docx (Word documents .docx)",
    "pypdf": "pypdf (PDF text extraction)",
    "openpyxl": "openpyxl (Excel .xlsx)",
    "PIL": "Pillow (images)",
    "pandas": "pandas (CSV / tabular data)",
    "striprtf": "striprtf (RTF documents)",
}


def _probe_doc_libs() -> tuple[list[str], list[str]]:
    installed: list[str] = []
    missing: list[str] = []
    for mod, label in _DOC_LIBS.items():
        try:
            importlib.import_module(mod)
            installed.append(label)
        except ImportError:
            missing.append(label)
    return installed, missing


def _load_memory_snapshot() -> str:
    """Best-effort: ship the agent's persistent memory into its system prompt.

    Tool-agent runs as a subprocess but shares the workspace (and therefore
    the agent-paths config dir) with the parent. Failures are swallowed so a
    missing memory file never blocks startup.
    """
    try:
        from tool.tool_memory import snapshot_for_system_prompt
        return snapshot_for_system_prompt() or ""
    except Exception:  # pragma: no cover - defensive
        return ""


# Per-tool one-liners. Composed dynamically based on the active tool set so
# read-only sessions don't see "you can run_command" advice the model can't
# act on.
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "read_file": "`read_file` — plain UTF-8 file read with workspace boundary check.",
    "write_file": "`write_file` — UTF-8 file write (creates parent dirs).",
    "list_directory": "`list_directory` — list files/subdirs under a workspace path.",
    "grep_search": "`grep_search` — ripgrep-style regex search across files.",
    "glob_search": "`glob_search` — find files by glob pattern (e.g. `**/*.py`).",
    "web_search": (
        "`web_search` — DuckDuckGo (or Tavily if TAVILY_API_KEY is set). "
        "Use to discover URLs when the user describes content without a link."
    ),
    "web_extract": (
        "`web_extract` — fetch a single URL and return readable text (no JS "
        "rendering). Use whenever the user pastes a URL and asks you to read "
        "/ summarize / quote / translate / 复述 it."
    ),
    "web_crawl": (
        "`web_crawl` — BFS across a small number of same-host pages. Use when "
        "one page isn't enough; otherwise prefer `web_extract`."
    ),
    "run_python": (
        "`run_python` — execute Python in a subprocess. Use for binary formats "
        "(.docx → python-docx, .pdf → pypdf, .xlsx → openpyxl, images → Pillow, "
        "CSV/tabular → pandas)."
    ),
    "run_command": (
        "`run_command` — shell commands. Use for CLI utilities, pipes, and "
        "inspecting binary headers; also `pip install <pkg>` when a library is "
        "missing."
    ),
    "clarify": (
        "`clarify` — ask the user one question. Use when a task is genuinely "
        "ambiguous and proceeding risks doing the wrong thing."
    ),
    "memory": (
        "`memory` — persist a fact about the user. Use when they share a "
        "name, preference, or fact about themselves, or ask you to "
        "remember / update / forget something. `action=add|replace|remove|read`, "
        "`target=user|memory`.\n"
        "  Flow example:\n"
        "    User: 我是 AI 专业的\n"
        "    You: (tool_call) memory(action=\"add\", target=\"user\", content=\"用户是 AI 专业的。\")\n"
        "    Tool: ok\n"
        "    You: 原来你学 AI 专业,挺有意思的方向。你对哪一块比较感兴趣?\n"
        "  IMPORTANT: the tool returns just `ok`; you must write the real "
        "  reply yourself based on what the user actually said. Don't echo "
        "  the tool result."
    ),
}


def _common_patterns_for(tool_names: set[str]) -> list[str]:
    """Pattern advice keyed on the available toolset. Lines whose preconditions
    aren't met (e.g. ``run_command`` not bound under read-only) are dropped so
    the model isn't told to invoke tools it can't actually call."""
    patterns: list[str] = []
    if "web_extract" in tool_names:
        patterns.append(
            "- User pastes a URL → `web_extract` it, then answer from the "
            "returned text. Do NOT tell the user you can't access URLs."
        )
    if "read_file" in tool_names:
        patterns.append("- Full file path given → read it directly. Skip discovery.")
    if "glob_search" in tool_names:
        patterns.append(
            "- Only a fragment known → `glob_search '**/*fragment*'`, then read."
        )
    if {"run_python", "run_command"} & tool_names:
        patterns.append(
            "- Text read fails with \"utf-8 … invalid byte\" → file is binary. "
            "Pick a reader: .docx → run_python with python-docx; .pdf → run_python "
            "with pypdf or `pdftotext` via run_command; .xlsx → openpyxl."
        )
    if "run_command" in tool_names:
        patterns.append(
            "- A required library is listed as Not installed below → install "
            "it with `run_command` (`pip install <pkg>`, timeout=180), then retry."
        )
    if "list_directory" in tool_names or "glob_search" in tool_names:
        patterns.append("- A wrong path → use `list_directory` or `glob_search` to find the file.")
    return patterns


def build_system_prompt(tool_names: list[str] | None = None) -> str:
    """Build the tool-agent system prompt for the given bound toolset.

    ``tool_names`` is the list of tool names the ReAct loop has actually
    bound (via ``tool_executor.make_langchain_tools(mode=...)``). The prompt
    only mentions tools in this set — under read-only mode, ``run_command``
    advice is omitted entirely so the model doesn't try to invoke a tool it
    can't call.

    Passing ``None`` falls back to the full toolset (legacy / unit-test
    callers that don't yet thread mode through).
    """
    if tool_names is None:
        tool_names = list(_TOOL_DESCRIPTIONS.keys())
    bound = set(tool_names)

    tools_block = "\n".join(
        f"- {_TOOL_DESCRIPTIONS[n]}" for n in tool_names if n in _TOOL_DESCRIPTIONS
    ) or "- (no tools available under the current permission mode)"

    installed, missing = _probe_doc_libs()
    lib_lines: list[str] = []
    if installed:
        lib_lines.append("- Installed (just import): " + "; ".join(installed))
    if missing and "run_command" in bound:
        # Only suggest pip install when run_command is actually bound — under
        # read-only the model can neither install nor use these libs, so
        # listing them is just noise.
        lib_lines.append(
            "- Not installed — `pip install` only if the task needs them: "
            + "; ".join(missing)
        )
    libs_block = "\n".join(lib_lines) or "- (no document libraries detected)"

    memory_snapshot = _load_memory_snapshot()
    memory_section = (
        f"\n## Persistent memory\n{memory_snapshot}\n" if memory_snapshot else ""
    )

    patterns = _common_patterns_for(bound)
    patterns_block = "\n".join(patterns) if patterns else "- (no patterns apply to the available tools)"

    # When the toolset is restricted (read-only or otherwise), tell the model
    # explicitly. Otherwise it may try to call tools it saw in training data.
    mode_note = ""
    full_set = set(_TOOL_DESCRIPTIONS.keys())
    if bound != full_set:
        missing_tools = sorted(full_set - bound)
        mode_note = (
            "\n## Mode-restricted toolset\nThe following tools are NOT "
            f"available this turn (user-selected permission mode): "
            f"{', '.join(missing_tools)}. If the task strictly requires one "
            f"of them, refuse with a clear message and suggest switching mode "
            f"via `/permissions`.\n"
        )

    return f"""\
You are a workspace + web specialist agent inside a CLI. You execute file
operations and web fetches and report concrete findings.

## Tools
{tools_block}
{mode_note}
## Environment
{libs_block}
{memory_section}
## Common patterns
{patterns_block}

## Termination rules
- When a `web_search`/`web_extract` result has `"retryable": false`, the
  failure is a hard wall (anti-scraping redirect loop, 404, DNS/connection
  failure). Do NOT retry URL variants or switch provider — read its
  `"advice"`, then answer from your own knowledge if the topic is
  well-known, or pivot to a genuinely different source. Each non-retryable
  failure burns ~25s on dead network paths; don't stack them.
- Same URL fails twice (403, redirect loop, anti-bot HTML, empty text) →
  STOP retrying that URL. Pivot to `web_search` for the topic, or answer
  from your own knowledge if the topic is well-known. Don't repeatedly
  request the same blocked endpoint.
- Hard cap: about 8 tool calls per task. If you haven't reached an answer
  by then, WRITE A FINAL ANSWER summarising (a) what the user wanted,
  (b) what you tried, (c) the best information you actually obtained,
  and (d) any limitation. Do not keep digging silently — the user would
  rather see a partial answer than a wall of failed tool calls.
- The final message must be plain text (no `tool_calls`). That's how the
  CLI knows the turn is finished.

## Output style
- Narrate one short sentence before each tool call so the user sees
  progress ("Fetching the page.", "Reading the file."). Skip the
  narration when the next step is obvious from context.
- **Tools return JSON. NEVER repeat that JSON back to the user as your
  final answer.** Extract the useful fields and answer naturally. After
  `memory(action="add")` succeeds, say "好的,记住了…" (one short sentence)
  -- the user does NOT want to see `{{"success": true, "entries": [...]}}`.
  After `read_file` returns, summarise or quote the relevant lines, not
  the wrapper struct. Apply this to every tool.
- {CONCISE_RULE}
- {LANGUAGE_RULE}
- {STOP_WHEN_DONE_RULE}
- {NO_RAW_TOOL_MARKUP_RULE}
"""


# Default prompt used by callers that don't thread a mode through (legacy
# single-agent loop, unit tests that construct ToolAgentLoop directly).
# Multi-agent path builds a mode-specific prompt per turn in ``ToolAgentLoop``.
SYSTEM_PROMPT = build_system_prompt()


class ToolAgentState(TypedDict, total=False):
    messages: list
    task: str
    tool_calls: int


class ToolAgentLoop:
    """LLM-powered ReAct loop wrapping a file-manipulation specialist agent."""

    def __init__(self, llm, tools: list, *, context: str = ""):
        self._llm = llm
        self._tools = tools
        # Orchestrator-supplied conversation snapshot — empty on a clean session
        # or when the caller didn't send one. Stored on the instance so the
        # bound ``_prompt_for_state`` callback can append it to the system
        # prompt every time langgraph asks for the prompt (which can happen
        # multiple times within a single ReAct turn).
        self._context = (context or "").strip()
        self._agent = None

    async def run(self, task: str) -> AsyncIterator[dict[str, Any]]:
        """Run the ReAct loop, yielding streaming events.

        Event types:
          {"type": "thinking"}
          {"type": "tool_call", "name": str, "args": dict}
          {"type": "tool_result", "name": str, "preview": str}
          {"type": "text", "chunk": str}
          {"type": "done", "text": str, "tool_calls": int}
          {"type": "error", "message": str}
        """
        agent = self._build_agent()
        yield {"type": "thinking"}

        stream_buffer = ""
        tool_calls_count = 0
        final_text = ""
        # True once we've seen a "terminal" AIMessage (content present AND no
        # tool_calls). That's the proper "I'm done" signal from the model.
        # If the loop exits without ever seeing one, the model spent the whole
        # turn calling tools and never wrote an answer — the user gets a
        # synthesized "I tried N tools but didn't reach a clear answer"
        # diagnostic instead of silence.
        terminal_answer_seen = False
        # langgraph's `stream_mode="values"` yields the WHOLE message list on
        # every state update — meaning a tool_call from step N is still present
        # in the snapshot at step N+1. Without de-duping, the orchestrator's
        # TUI redraws each `⏺ tool` header (and its result block) once per
        # subsequent values event, so a single write_file appears 2-3 times.
        # Track which tool_call ids and ToolMessage ids we have already
        # surfaced and skip them on later snapshots.
        seen_tool_call_ids: set[str] = set()
        seen_tool_result_ids: set[str] = set()
        # Raw content of every tool result surfaced this turn. Used to catch the
        # case where a smaller model ends the turn by pasting a tool's JSON
        # return verbatim as its "answer" (write_file's {ok, action, path, bytes}
        # is the classic culprit) — we rewrite that into a natural confirmation
        # so the user sees "已保存到 …" instead of raw JSON. See
        # ``_humanize_tool_echo``.
        tool_result_contents: list[str] = []
        # Per-AIMessage stream-progress tracker. Used to detect and collapse
        # two kinds of duplicate text events:
        #   1. Some providers (DeepSeek's flash variants, some local llama
        #      proxies, reasoning-mode toggles) stream CUMULATIVE chunks —
        #      every chunk carries the full assistant message so far, not a
        #      delta. The next chunk is `prev + new_token`, and yielding it
        #      as-is repeats everything already on screen.
        #   2. langgraph occasionally re-emits a completed AIMessage when
        #      transitioning out of the agent node, producing an identical
        #      chunk back-to-back.
        # Tracking content-so-far per message id lets us yield only the real
        # delta in both cases.
        seen_per_message: dict[str, str] = {}

        try:
            async for event in agent.astream(
                {"messages": [HumanMessage(content=task)]},
                config={
                    "configurable": {"thread_id": "tool-agent-turn"},
                    # Hard cap on graph steps. With ReAct, each tool call costs
                    # ~2 steps (plan + act), so 30 ≈ 14 tool-call rounds.
                    # Bumped from 15 because realistic web tasks (multiple
                    # retries on a flaky endpoint + a fallback web_search +
                    # the final answer) routinely brushed against the old cap
                    # and triggered an orchestrator-level re-plan AFTER the
                    # answer had already been streamed to the user.
                    "recursion_limit": 30,
                },
                stream_mode=["messages", "values"],
            ):
                mode, payload = (
                    event if isinstance(event, tuple) and len(event) == 2
                    else ("values", event)
                )

                if mode == "messages":
                    chunk, _metadata = payload
                    if is_langgraph_tool_chunk(chunk):
                        continue
                    if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
                        continue
                    token = self._chunk_text(chunk)
                    if not token:
                        continue
                    if has_raw_tool_markup(token):
                        continue

                    # Cross-message backstop for STRICTLY EXTENDING chunks.
                    # Some providers (DeepSeek's flash variants, Qwen-thinking,
                    # certain local llama proxies) stream CUMULATIVE chunks AND
                    # rotate ``msg.id`` between them, so the per-message
                    # tracker below misses the duplication and the UI ends up
                    # rendering the full answer once per chunk. When the new
                    # token strictly extends what we've already yielded, treat
                    # it as a continuation and emit only the suffix.
                    #
                    # Important: we do NOT skip ``token == stream_buffer``
                    # here. That case can be legitimate (two genuinely distinct
                    # AIMessages happening to carry identical content); the
                    # per-message dedup below handles the more common langgraph
                    # re-emission of the same AIMessage by id.
                    if stream_buffer and token != stream_buffer and token.startswith(stream_buffer):
                        delta = token[len(stream_buffer):]
                        if not delta:
                            continue
                        stream_buffer = token
                        msg_id = getattr(chunk, "id", None) or "__no_id__"
                        seen_per_message[msg_id] = token
                        if not _should_withhold_json(seen_per_message[msg_id], tool_result_contents):
                            yield {"type": "text", "chunk": delta}
                        continue

                    # Per-message dedup. See `seen_per_message` comment above for
                    # the two failure modes this protects against. Falls back to
                    # `__no_id__` when the provider gives us no message id —
                    # collisions across messages are acceptable because we only
                    # ever shrink the emitted text, never duplicate it.
                    msg_id = getattr(chunk, "id", None) or "__no_id__"
                    prev = seen_per_message.get(msg_id, "")
                    if token == prev:
                        # langgraph re-emitted an already-streamed chunk verbatim.
                        continue
                    if prev and token.startswith(prev):
                        # Cumulative chunk — yield only the new suffix.
                        delta = token[len(prev):]
                        seen_per_message[msg_id] = token
                        stream_buffer += delta
                        if not _should_withhold_json(seen_per_message[msg_id], tool_result_contents):
                            yield {"type": "text", "chunk": delta}
                        continue
                    # Standard delta-style chunk.
                    seen_per_message[msg_id] = prev + token
                    stream_buffer += token
                    if not _should_withhold_json(seen_per_message[msg_id], tool_result_contents):
                        yield {"type": "text", "chunk": token}

                elif mode == "values":
                    messages = payload.get("messages", [])
                    for msg in messages:
                        tool_calls = getattr(msg, "tool_calls", None)
                        if tool_calls:
                            for tc in tool_calls:
                                tc_id = (
                                    tc.get("id")
                                    or f"{getattr(msg, 'id', id(msg))}:{tc.get('name','')}"
                                )
                                if tc_id in seen_tool_call_ids:
                                    continue
                                seen_tool_call_ids.add(tc_id)
                                name = tc.get("name", "unknown")
                                args = tc.get("args", {})
                                tool_calls_count += 1
                                yield {"type": "tool_call", "name": name, "args": args}

                    for msg in messages:
                        if isinstance(msg, ToolMessage):
                            result_id = (
                                getattr(msg, "tool_call_id", None)
                                or getattr(msg, "id", None)
                                or str(id(msg))
                            )
                            if result_id in seen_tool_result_ids:
                                continue
                            seen_tool_result_ids.add(result_id)
                            content = str(getattr(msg, "content", "") or "")
                            tool_result_contents.append(content)
                            preview = _truncate_preview(content)
                            yield {
                                "type": "tool_result",
                                "name": getattr(msg, "name", "tool"),
                                "preview": preview,
                            }

                    for msg in messages:
                        if isinstance(msg, AIMessage) and msg.content and not getattr(msg, "tool_calls", None):
                            final_text = str(msg.content)
                            terminal_answer_seen = True

        except Exception as exc:
            # Late exception. Three sub-cases:
            #   1. Terminal AIMessage already seen  → clean done.
            #   2. Only intermediate narration in stream_buffer → emit a
            #      diagnostic so the user knows the turn ended inconclusively.
            #   3. Nothing captured → propagate as error (orchestrator retries).
            if terminal_answer_seen:
                answer = _humanize_tool_echo(final_text, tool_result_contents, task)
                if answer and _should_withhold_json(final_text, tool_result_contents):
                    # The raw JSON answer was withheld from the live stream;
                    # paint the humanized version so the CLI (text events only)
                    # isn't left blank.
                    yield {"type": "text", "chunk": answer}
                yield {"type": "done", "text": answer, "tool_calls": tool_calls_count}
                return
            partial = stream_buffer.strip()
            if partial:
                log.warning(
                    "ToolAgentLoop interrupted with only intermediate narration "
                    "(%d chars): %s",
                    len(partial), exc,
                )
                diag = _no_answer_diagnostic(tool_calls_count, reason="interrupted")
                # Stream the diagnostic so the orchestrator UI actually paints
                # it. `done.text` alone is used for state recording — only
                # `text` events reach the live screen.
                yield {"type": "text", "chunk": "\n\n" + diag}
                yield {
                    "type": "done",
                    "text": partial + "\n\n" + diag,
                    "tool_calls": tool_calls_count,
                }
                return
            log.exception("ToolAgentLoop error")
            yield {"type": "error", "message": str(exc)}
            return

        # Normal-exit path. Three outcomes mirror the exception path:
        #   1. Saw a terminal AIMessage  → emit it as-is.
        #   2. No terminal but some narration streamed → stream the diagnostic
        #      so the user sees it, then close with done.
        #   3. Nothing at all → stream the diagnostic alone, then done.
        if terminal_answer_seen:
            answer = _humanize_tool_echo(final_text, tool_result_contents, task)
            if answer and _should_withhold_json(final_text, tool_result_contents):
                # The raw JSON answer was withheld from the live stream; paint
                # the humanized version so the CLI (text events only) isn't
                # left blank.
                yield {"type": "text", "chunk": answer}
            yield {"type": "done", "text": answer, "tool_calls": tool_calls_count}
            return

        partial = stream_buffer.strip()
        diag = _no_answer_diagnostic(tool_calls_count, reason="no_terminal_answer")
        if partial:
            yield {"type": "text", "chunk": "\n\n" + diag}
            yield {
                "type": "done",
                "text": partial + "\n\n" + diag,
                "tool_calls": tool_calls_count,
            }
        else:
            yield {"type": "text", "chunk": diag}
            yield {"type": "done", "text": diag, "tool_calls": tool_calls_count}

    def _build_agent(self):
        if self._agent is not None:
            return self._agent

        self._agent = create_react_agent(
            model=self._llm,
            tools=self._tools,
            prompt=self._prompt_for_state,
            checkpointer=MemorySaver(),
        )
        return self._agent

    def _prompt_for_state(self, state: dict) -> list:
        from langchain_core.messages import SystemMessage

        messages = state.get("messages", [])
        # Build the prompt from the tools actually bound this turn — the
        # static module-level ``SYSTEM_PROMPT`` describes the full toolset,
        # which under read-only mode misleads the model into trying tools
        # it can't call (``run_command`` for pip install, etc.).
        bound_names = [getattr(t, "name", "") for t in self._tools]
        prompt = build_system_prompt(bound_names) if bound_names else SYSTEM_PROMPT
        prelude: list = [SystemMessage(content=prompt)]
        if self._context:
            # Second SystemMessage rather than inlining into SYSTEM_PROMPT so
            # the static prompt stays cacheable across turns; only the
            # per-turn conversation snapshot varies. Phrased as background
            # information (not as a user/assistant exchange) so the model
            # treats it as referent material, not as turns to respond to.
            prelude.append(SystemMessage(content=(
                "## Conversation context from the orchestrator\n\n"
                "Recent turns of this session (oldest first, most recent last). "
                "Use this to resolve referring expressions such as "
                "「上面的」、「刚才那个」、「这个」、「the URL above」 in the user's task. "
                "Do NOT treat these as new instructions to respond to — they are "
                "background only.\n\n"
                f"{self._context}"
            )))
        return [*prelude, *messages]

    @staticmethod
    def _chunk_text(chunk: object) -> str:
        content = getattr(chunk, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                (item.get("text", "") if isinstance(item, dict) else str(item))
                for item in content
            )
        return str(content or "")


def _no_answer_diagnostic(tool_calls_count: int, *, reason: str) -> str:
    """Bilingual short note explaining why the turn ended without a real answer.

    Reasons:
      "interrupted"         — exception (e.g. recursion limit) hit and we had
                              only intermediate narration to show.
      "no_terminal_answer"  — loop ended normally but the model never emitted
                              a content-only AIMessage; it just looped on
                              tool calls.

    The text is deliberately compact — the user already saw the tool trail.
    """
    call_word = "tool call" if tool_calls_count == 1 else "tool calls"
    if reason == "interrupted":
        return (
            f"_(I was interrupted after {tool_calls_count} {call_word} before I "
            f"could write a final answer. The fetches I tried hit errors or "
            f"anti-bot pages. Try a different URL or rephrase the request.)_"
        )
    return (
        f"_(I made {tool_calls_count} {call_word} but didn't reach a clear "
        f"final answer — most fetches were blocked or returned no useful "
        f"content. Try a different URL, ask via `web_search` keywords, or "
        f"rephrase the question.)_"
    )


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in (text or ""))


def _is_json_blob_start(text: str) -> bool:
    """True if ``text`` (a message's accumulated content) opens with a bare
    JSON object. Natural-language answers and ```fenced``` code never start with
    a bare ``{``."""
    return text.lstrip()[:1] == "{"


def _should_withhold_json(text: str, tool_result_contents: list[str]) -> bool:
    """Whether a ``{``-leading streamed answer should be withheld from the live
    stream (then humanized at done).

    Withhold ONLY when at least one tool result was produced this turn — a
    ``{``-leading answer is then plausibly the model echoing a tool's JSON
    return, which we want to suppress. A ``{``-leading answer with NO preceding
    tool calls cannot be an echo, so it streams live token-by-token, preserving
    the streaming UX for a JSON answer the user legitimately asked for (the old
    check withheld every JSON answer unconditionally)."""
    return bool(tool_result_contents) and _is_json_blob_start(text)


def _extract_leading_json_object(text: str) -> str | None:
    """Return the leading balanced-brace ``{...}`` substring of ``text``, or
    None if it doesn't start with one. Used to recognise a tool-JSON echo even
    when the model appends trailing characters (a period, "Done.", a stray code
    fence) that would defeat a plain ``endswith('}')`` check. Brace matching
    ignores braces inside JSON strings."""
    s = (text or "").strip()
    if not s.startswith("{"):
        return None
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[: i + 1]
    return None


def _confirm_save(data: dict, task: str) -> str:
    """Natural-language confirmation built from a write_file tool result."""
    cjk = _has_cjk(task)
    path = data.get("path")
    nbytes = data.get("bytes")
    updated = data.get("action") == "update"
    if cjk:
        size = f"（{nbytes} 字节）" if isinstance(nbytes, int) else ""
        return f"{'已更新' if updated else '已保存到'} {path}{size}。"
    size = f" ({nbytes} bytes)" if isinstance(nbytes, int) else ""
    return f"{'Updated' if updated else 'Saved to'} {path}{size}."


def _humanize_tool_echo(
    final_text: str, tool_result_contents: list[str], task: str
) -> str:
    """Rewrite a final answer that is just a tool's raw JSON return.

    Despite the system prompt's "never repeat tool JSON" rule and the terse
    ``write_file`` return, smaller models still occasionally end a turn by
    pasting a tool result verbatim (e.g. ``{"ok": true, "action": "create",
    "path": "...", "bytes": 2775}``), so the user sees raw JSON instead of a
    confirmation. When the final answer is such an echo, turn it into a natural
    one-liner; otherwise return it unchanged.

    Robust to mangled echoes: a model re-emitting a Windows path almost always
    collapses ``\\\\`` to ``\\``, which makes the echo invalid JSON — so we do
    NOT rely on parsing the model's text. Instead we match it against the tool
    results we actually returned this turn (those are always valid JSON, we
    produced them) under a backslash/whitespace-insensitive normalization, and
    build the confirmation from that trustworthy copy.

    Conservative: only fires when the final answer is a ``{...}`` blob that
    matches a write_file result we returned. A legitimate JSON answer the user
    asked for matches no tool result and is left intact.
    """
    # Extract the leading ``{...}`` object. Tolerates trailing characters the
    # model sometimes appends after the echo (a period, "Done.", a code fence) —
    # the old ``endswith('}')`` check returned the raw text unchanged in those
    # cases, leaking JSON to the user.
    candidate = _extract_leading_json_object(final_text)
    if candidate is None:
        return final_text  # natural-language answer — leave it alone

    def _norm(s: str) -> str:
        # Drop whitespace AND backslashes so a mangled-escape Windows-path echo
        # still equals the valid tool-result JSON it was copied from.
        return "".join(s.split()).replace("\\", "")

    norm_final = _norm(candidate)

    for tr in tool_result_contents:
        try:
            data = json.loads(tr)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("path"):
            continue
        # Echo iff the blob equals a result we returned, verbatim modulo
        # escaping/whitespace. ``_norm`` strips ALL backslashes from both
        # sides, so a model that collapsed ``\\`` to ``\`` while echoing a
        # Windows path still matches — no need for a looser filename-substring
        # check, which could misfire on a JSON answer the user actually asked
        # for.
        if _norm(tr) == norm_final:
            return _confirm_save(data, task)

    # No tool result to lean on, but the blob itself is a clean write signature.
    try:
        data = json.loads(candidate)
        if isinstance(data, dict) and "ok" in data and "path" in data:
            return _confirm_save(data, task)
    except (json.JSONDecodeError, ValueError):
        pass

    return final_text


def _truncate_preview(content: str, max_len: int = 200) -> str:
    if len(content) <= max_len:
        return content
    return content[:max_len] + "…"


def default_llm():
    """Build an LLM from active config, or return a mock for tests."""
    import os

    try:
        from config import build_llm, hydrate_env_from_credentials, load_active_config

        hydrate_env_from_credentials()
        return build_llm(load_active_config())
    except Exception:
        from agents.shared.mock_chat_model import MockChatModel

        return MockChatModel.from_env("MOCK_TOOL_AGENT_SCRIPT", default="ok")
