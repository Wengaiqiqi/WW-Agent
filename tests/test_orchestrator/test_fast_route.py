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
