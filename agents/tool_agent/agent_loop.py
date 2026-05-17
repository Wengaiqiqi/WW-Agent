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


def _build_system_prompt() -> str:
    installed, missing = _probe_doc_libs()
    lib_lines = []
    if installed:
        lib_lines.append("Already installed (just import and use): " + "; ".join(installed) + ".")
    if missing:
        lib_lines.append(
            "NOT installed — only `pip install` if the user's task truly needs them: "
            + "; ".join(missing) + "."
        )
    lib_block = "\n".join(lib_lines)
    return (
        "You are a file-system specialist agent. Available tools:\n"
        "- read_file, write_file, list_directory, grep_search, glob_search\n"
        "  These read/write plain UTF-8 text and search the workspace.\n"
        "- run_python: execute Python code in a subprocess. Use this when the "
        "  text tools cannot handle a file format — read .docx with python-docx, "
        "  .pdf with pypdf, .xlsx with openpyxl, images with Pillow, etc.\n"
        "- run_command: execute a shell command. Use this for CLI utilities, "
        "  pipes, inspecting binary file headers, or anything that's easier in "
        "  the shell than in Python.\n\n"
        f"PYTHON ENVIRONMENT (probed at startup):\n{lib_block}\n\n"
        "BE DIRECT. The user is in a hurry. Pick the fewest tool calls that "
        "complete the task. For 'view X file' or 'read X':\n"
        "  1) glob_search '**/*X*' to locate the file IF the path is unknown — "
        "  do NOT list_directory the cwd first.\n"
        "  2) read_file if it's text, or run_python if it's binary.\n"
        "  3) Answer.\n"
        "If the user gave a full path, skip step 1 entirely. Don't double-check, "
        "don't enumerate adjacent files, don't search the whole project unless "
        "the task explicitly requires it.\n\n"
        "NARRATE WHILE YOU WORK. Before each tool call, write ONE short sentence "
        "(in the user's language) saying what you're about to do. Examples:\n"
        "- '好的，我先搜一下这个文件。' → glob_search\n"
        "- '找到了 张某.docx，让我读一下。' → read_file\n"
        "- '是二进制的 docx，我用 python-docx 提取文字。' → run_python\n"
        "This narration is streamed to the user — keep each sentence short and "
        "natural. Don't repeat the file content; just say what you're doing next.\n\n"
        "REFLECTION LOOP: when a tool errors, READ the error carefully and try a "
        "different approach. Examples:\n"
        "- read_file fails with 'utf-8 ... invalid byte' → file is binary. Pick "
        "  the right reader: .docx → run_python with python-docx; .pdf → run_python "
        "  with pypdf or `pdftotext` via run_command; .xlsx → openpyxl; otherwise "
        "  inspect with `file <path>` via run_command.\n"
        "- If a Python library you need is listed as NOT installed above, run "
        "  `pip install <pkg>` via run_command with timeout=180 and retry. "
        "  Skip this step entirely for libraries listed as already installed.\n"
        "- If a path is wrong, use list_directory or glob_search to find the file.\n\n"
        "Keep iterating with different tools/approaches until the task is done, or "
        "you can give a clear explanation of why it cannot be done. When a tool "
        "returns JSON, extract the useful content and present it naturally in your "
        "final answer. Reply in the same language the user used.\n\n"
        "STOP WHEN DONE. The moment the requested action completes successfully "
        "(write_file returned, content extracted, search yielded results), give "
        "the final answer. Do NOT re-read the file to count characters, do NOT "
        "run extra subprocess checks for self-verification, do NOT explore "
        "neighboring files. Speed matters — the user expects single-agent-like "
        "responsiveness. Verify only what the user explicitly asked you to verify."
    )


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
                    # ~2 steps (plan + act), so 15 ≈ 7 tool-call rounds. Without
                    # this, a slow/over-thinking model can stay in the loop
                    # indefinitely and never emit a `done` event.
                    "recursion_limit": 15,
                },
                stream_mode=["messages", "values"],
            ):
                mode, payload = (
                    event if isinstance(event, tuple) and len(event) == 2
                    else ("values", event)
                )

                if mode == "messages":
                    chunk, _metadata = payload
                    if self._is_tool_chunk(chunk):
                        continue
                    if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
                        continue
                    token = self._chunk_text(chunk)
                    if not token:
                        continue
                    if _has_raw_tool_markup(token):
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

        except Exception as exc:
            log.exception("ToolAgentLoop error")
            yield {"type": "error", "message": str(exc)}
            return

        if not final_text and stream_buffer.strip():
            final_text = stream_buffer.strip()

        yield {"type": "done", "text": final_text, "tool_calls": tool_calls_count}

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
    def _is_tool_chunk(chunk: object) -> bool:
        chunk_type = getattr(chunk, "type", "")
        if chunk_type in {"tool", "ToolMessage", "tool_message"}:
            return True
        return "tool" in chunk.__class__.__name__.lower()

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


def _has_raw_tool_markup(content: str) -> bool:
    return any(m in content for m in ("<tool_call>", "<function=", "<parameter="))


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
