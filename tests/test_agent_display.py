"""Tests for the shared tool-display helpers.

These guard against the duplicate-drift bug that motivated the extraction:
legacy and orchestrator had separate inline copies of TOOL_ARG_PRIMARY_KEY
with slightly different entries (and ``memory`` was even mismapped to
``operation`` in one of them). Single source of truth now lives in
``agent_display``; these tests pin the key mappings against the real tool
schemas in ``tool_executor._TOOL_MAP`` (the live dispatch surface).
"""
from __future__ import annotations

import pytest

from agent_display import (
    TOOL_ARG_PRIMARY_KEY,
    extract_message_text,
    format_tool_arg_summary,
    has_raw_tool_markup,
    is_langgraph_tool_chunk,
)
from agents.tool_agent.tool_executor import _TOOL_MAP


# ---------------------------------------------------------------------------
# Primary-key mapping vs. real tool schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name", sorted(set(TOOL_ARG_PRIMARY_KEY) & set(_TOOL_MAP))
)
def test_primary_key_matches_tool_schema(tool_name):
    """The primary-arg name in TOOL_ARG_PRIMARY_KEY must be a real parameter in
    the tool's input schema. Regression for the previous ``memory → operation``
    mismapping (real schema property is ``action``)."""
    _handler, schema, _desc = _TOOL_MAP[tool_name]
    props = schema.get("properties", {})
    declared = TOOL_ARG_PRIMARY_KEY[tool_name]
    assert declared in props, (
        f"TOOL_ARG_PRIMARY_KEY[{tool_name!r}] = {declared!r} but the tool's "
        f"schema properties are {sorted(props)}"
    )


def test_every_primary_key_tool_exists_in_dispatch_map():
    """No stale display entries: every tool named in TOOL_ARG_PRIMARY_KEY must
    still be a real dispatchable tool (catches retired tools left behind)."""
    stale = set(TOOL_ARG_PRIMARY_KEY) - set(_TOOL_MAP)
    assert not stale, f"TOOL_ARG_PRIMARY_KEY names tools no longer in _TOOL_MAP: {sorted(stale)}"


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


def test_extract_message_text_ignores_non_text_content_blocks():
    class TextBlock:
        type = "text"
        text = "second"

    content = [
        {"type": "thinking", "thinking": "private", "signature": "signed"},
        {"type": "text", "text": "first"},
        TextBlock(),
    ]

    assert extract_message_text(content) == "firstsecond"
