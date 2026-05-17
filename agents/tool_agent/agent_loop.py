"""LLM-powered ReAct agent loop for tool-agent.

Uses LangGraph's create_react_agent to stream tool-calling iterations,
yielding typed event dicts the orchestrator's TUI consumes.
"""
from __future__ import annotations

import importlib
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


def _build_system_prompt() -> str:
    installed, missing = _probe_doc_libs()
    lib_lines: list[str] = []
    if installed:
        lib_lines.append("- Installed (just import): " + "; ".join(installed))
    if missing:
        lib_lines.append(
            "- Not installed — `pip install` only if the task needs them: "
            + "; ".join(missing)
        )
    libs_block = "\n".join(lib_lines) or "- (no document libraries detected)"

    memory_snapshot = _load_memory_snapshot()
    memory_section = (
        f"\n## Persistent memory\n{memory_snapshot}\n" if memory_snapshot else ""
    )

    return f"""\
You are a workspace + web specialist agent inside a CLI. You execute file
operations and web fetches and report concrete findings.

## Tools
- `read_file` / `write_file` / `list_directory` / `grep_search` /
  `glob_search` — plain UTF-8 reads/writes and workspace search.
- `web_extract` — fetch a single URL and return readable text (no JS
  rendering). Use this whenever the user pastes a URL and asks you to
  read / summarize / quote / translate / 复述 it.
- `web_search` — DuckDuckGo (or Tavily if TAVILY_API_KEY is set). Use to
  discover URLs when the user describes content without a link.
- `web_crawl` — BFS across a small number of same-host pages. Use when a
  single page isn't enough; otherwise prefer `web_extract`.
- `run_python` — execute Python in a subprocess. Use for binary formats
  (.docx → python-docx, .pdf → pypdf, .xlsx → openpyxl, images → Pillow,
  CSV/tabular → pandas).
- `run_command` — shell commands. Use for CLI utilities, pipes, and
  inspecting binary headers.

## Environment
{libs_block}
{memory_section}
## Common patterns
- User pastes a URL → `web_extract` it, then answer from the returned
  text. Do NOT tell the user you can't access URLs; you have web_extract.
- Full file path given → read it directly. Skip discovery.
- Only a fragment known → `glob_search '**/*fragment*'`, then read.
- Text read fails with "utf-8 … invalid byte" → file is binary. Pick a
  reader: .docx → run_python with python-docx; .pdf → run_python with
  pypdf or `pdftotext` via run_command; .xlsx → openpyxl.
- A required library is listed as Not installed above → install it with
  `run_command` (`pip install <pkg>`, timeout=180), then retry.
- A wrong path → use `list_directory` or `glob_search` to find the file.

## Termination rules
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
- Tools return JSON — extract the useful fields and answer naturally, do
  not paste raw JSON at the user.
- {CONCISE_RULE}
- {LANGUAGE_RULE}
- {STOP_WHEN_DONE_RULE}
- {NO_RAW_TOOL_MARKUP_RULE}
"""


# Built once at module import; the installed-libs set doesn't change at runtime.
SYSTEM_PROMPT = _build_system_prompt()


class ToolAgentState(TypedDict, total=False):
    messages: list
    task: str
    tool_calls: int


class ToolAgentLoop:
    """LLM-powered ReAct loop wrapping a file-manipulation specialist agent."""

    def __init__(self, llm, tools: list):
        self._llm = llm
        self._tools = tools
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
                        yield {"type": "text", "chunk": delta}
                        continue
                    # Standard delta-style chunk.
                    seen_per_message[msg_id] = prev + token
                    stream_buffer += token
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
                yield {"type": "done", "text": final_text, "tool_calls": tool_calls_count}
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
            yield {"type": "done", "text": final_text, "tool_calls": tool_calls_count}
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

    @staticmethod
    def _prompt_for_state(state: dict) -> list:
        from langchain_core.messages import SystemMessage

        messages = state.get("messages", [])
        return [SystemMessage(content=SYSTEM_PROMPT), *messages]

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
