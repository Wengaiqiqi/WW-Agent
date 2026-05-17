"""Tests for the shared tool-display helpers.

These guard against the duplicate-drift bug that motivated the extraction:
legacy and orchestrator had separate inline copies of TOOL_ARG_PRIMARY_KEY
with slightly different entries (and ``memory`` was even mismapped to
``operation`` in one of them). Single source of truth now lives in
``agent_display``; these tests pin the key mappings against the real
``@tool`` signatures in ``tool/tools.py``.
"""
from __future__ import annotations

import inspect

import pytest

from agent_display import (
    TOOL_ARG_PRIMARY_KEY,
    format_tool_arg_summary,
    has_raw_tool_markup,
    is_langgraph_tool_chunk,
)


# ---------------------------------------------------------------------------
# Primary-key mapping vs. real tool signatures
# ---------------------------------------------------------------------------


def _first_param(tool_callable) -> str:
    """Return the first positional argument name of a LangChain @tool callable."""
    fn = getattr(tool_callable, "func", None) or tool_callable
    sig = inspect.signature(fn)
    params = [
        p for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    return params[0].name if params else ""


@pytest.mark.parametrize("tool_name", [
    "read_file", "write_file", "edit_file",
    "list_directory", "glob_search", "grep_search",
    "apply_patch", "run_command", "run_python",
    "web_search", "web_extract", "web_crawl",
    "memory", "clarify", "todo_write", "calculator",
    "osv_check", "home_assistant", "x_search",
    "vision_analyze", "mixture_of_agents",
])
def test_primary_key_matches_tool_signature(tool_name):
    """The primary-arg name in TOOL_ARG_PRIMARY_KEY must actually exist on the
    tool's signature. Regression for the previous ``memory → operation``
    mismapping (real signature is ``memory(action=..., ...)``)."""
    from tool import tools as tools_mod

    tool_callable = getattr(tools_mod, tool_name)
    fn = getattr(tool_callable, "func", None) or tool_callable
    sig = inspect.signature(fn)
    declared = TOOL_ARG_PRIMARY_KEY.get(tool_name)
    assert declared, f"{tool_name} not in TOOL_ARG_PRIMARY_KEY"
    assert declared in sig.parameters, (
        f"TOOL_ARG_PRIMARY_KEY[{tool_name!r}] = {declared!r} but the real "
        f"signature is {tool_name}({', '.join(sig.parameters)})"
    )


# ---------------------------------------------------------------------------
# format_tool_arg_summary
# ---------------------------------------------------------------------------


def test_format_picks_primary_key_value():
    out = format_tool_arg_summary("read_file", {"path": "/tmp/x.txt", "offset": 0})
    assert out == "/tmp/x.txt"


def test_format_truncates_long_value():
    out = format_tool_arg_summary("run_command", {"command": "x" * 200}, max_width=40)
    # 37 'x' chars + the single-char "…" = 38 chars total. We just assert
    # the output stays at or below the budget and ends with the ellipsis.
    assert len(out) <= 40
    assert out.endswith("…")


def test_format_takes_first_line_only():
    out = format_tool_arg_summary("run_python", {"code": "import sys\nprint(sys.path)"})
    assert out == "import sys"


def test_format_falls_back_to_kv_for_unknown_tool():
    out = format_tool_arg_summary("brand_new_tool", {"x": 1, "y": "value", "z": "third"})
    # First two args only (py3.7+ preserves dict insertion order). Strings
    # pass through unquoted; non-strings go through ``repr()``.
    assert "x=1" in out
    assert "y=value" in out
    assert "z=" not in out  # third is dropped


def test_format_empty_args_returns_empty():
    assert format_tool_arg_summary("read_file", {}) == ""


# ---------------------------------------------------------------------------
# has_raw_tool_markup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Sure I'll <tool_call> read it.",
    "Let me <function=read_file>",
    "Calling <parameter=path>...",
])
def test_has_raw_markup_positive(text):
    assert has_raw_tool_markup(text)


@pytest.mark.parametrize("text", [
    "",
    "a normal sentence",
    "discussion of <details> HTML tag",
    "regular code: function() { return; }",
])
def test_has_raw_markup_negative(text):
    assert not has_raw_tool_markup(text)


# ---------------------------------------------------------------------------
# is_langgraph_tool_chunk
# ---------------------------------------------------------------------------


def test_is_tool_chunk_by_type_attr():
    class _Chunk:
        type = "tool"
    assert is_langgraph_tool_chunk(_Chunk())


def test_is_tool_chunk_by_class_name():
    class ToolMessageChunk:
        type = ""
    assert is_langgraph_tool_chunk(ToolMessageChunk())


def test_is_not_tool_chunk_for_ai_message():
    class _AIChunk:
        type = "AIMessageChunk"
    assert not is_langgraph_tool_chunk(_AIChunk())
