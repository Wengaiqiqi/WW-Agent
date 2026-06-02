"""Unit tests for ``orchestrator.fast_route.fast_route`` — the local heuristic
that delegates obvious tool-agent work without a planner LLM round-trip.

Focus: the over-broad bare-``startswith``-over-words match used to mis-route
ordinary prose that merely begins with a tool verb's letters
("updates are great" -> startswith "update"). These regression tests pin that
such chat falls through to the planner while genuine imperatives still route.
"""
from __future__ import annotations

import pytest

from orchestrator.fast_route import fast_route

CAPS = ["tool.task"]


def _routes(text: str) -> bool:
    return fast_route(text, capabilities=CAPS) is not None


@pytest.mark.parametrize("text", [
    "updates are great news for everyone",   # startswith "update"
    "fixated on this one idea",               # startswith "fix"
    "reading is fun",                          # startswith "read"
    "searching questions deserve answers",    # startswith "search"
    "editorial standards matter",             # startswith "edit"
])
def test_prose_beginning_with_a_verbs_letters_is_not_routed(text):
    assert not _routes(text), f"should fall through to planner: {text!r}"


@pytest.mark.parametrize("text", [
    "fix the login bug",            # "fix " prefix
    "update the README",           # "update " prefix
    "read config.py",              # "read " prefix + .py marker
    "please review the diff",      # whole word "review"
    "can you optimize this loop",  # whole word "optimize"
])
def test_genuine_imperatives_still_route(text):
    assert _routes(text), f"should fast-route to tool.task: {text!r}"


def test_routed_decision_shape():
    dec = fast_route("read config.py", capabilities=CAPS)
    assert dec == {"capability": "tool.task", "arguments": {"task": "read config.py"}}


# --- Phase D: lock the guards the design relies on (harden, no behavior change) ---


def test_disable_escape_hatch_forces_planner(monkeypatch):
    """LANGCHAIN_AGENT_DISABLE_FAST_ROUTE=1 makes even an obvious tool task
    fall through to the planner — the documented escape hatch must stay."""
    monkeypatch.setenv("LANGCHAIN_AGENT_DISABLE_FAST_ROUTE", "1")
    assert fast_route("read config.py", capabilities=CAPS) is None


def test_disable_unset_routes_normally(monkeypatch):
    monkeypatch.delenv("LANGCHAIN_AGENT_DISABLE_FAST_ROUTE", raising=False)
    assert fast_route("read config.py", capabilities=CAPS) is not None


@pytest.mark.parametrize("text", [
    "查看 README",   # CJK read verb
    "运行 pytest",   # CJK run verb (full-access only)
])
def test_cjk_imperatives_route_in_full_access(text):
    assert fast_route(text, capabilities=CAPS, mode="danger-full-access") is not None


def test_readonly_mode_routes_read_verbs_but_not_write_verbs():
    # A read-class request still skips the planner under read-only...
    assert fast_route("查看 README", capabilities=CAPS, mode="read-only") is not None
    assert fast_route("read config.py", capabilities=CAPS, mode="read-only") is not None
    # ...but a write-class request defers to the planner (it can't succeed
    # anyway; the planner renders a clean prose refusal).
    assert fast_route("保存到 a.txt", capabilities=CAPS, mode="read-only") is None
    assert fast_route("delete the cache", capabilities=CAPS, mode="read-only") is None


@pytest.mark.parametrize("text", [
    "总结上面的内容",          # CJK "the above"
    "fix the bug from the previous message",
    "what you just wrote",
])
def test_referring_expressions_defer_to_planner(text):
    # Only the planner sees session history, so a referent to earlier
    # conversation must NOT be fast-routed to the context-blind tool-agent.
    assert fast_route(text, capabilities=CAPS) is None


def test_capability_prefixed_message_defers_to_planner():
    # "CAP:arg" direct-dispatch syntax for a known capability is left to the
    # planner rather than wrapped as a tool.task.
    assert fast_route("tool.task: do the thing", capabilities=CAPS) is None
