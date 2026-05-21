"""Shared permission-mode whitelist.

Lives in ``agents.shared`` so both the orchestrator (which signs grants) and
the agents (which mint sub-grants for downstream tools) can read the same
allow-list without one process having to reach across the layering boundary.
Previously the constant lived in ``orchestrator/permission_gate.py`` and
``skill-agent`` imported it from there — a reverse-direction import that
made the layered architecture less honest than it looked.
"""
from __future__ import annotations


class PermissionDenied(Exception):
    """Raised when a tool call is refused by either the outer gate (the
    orchestrator's PermissionGate) or the inner gate (a skill trying to mint
    a sub-grant for a tool the user's mode doesn't actually permit)."""


_MODE_WHITELIST: dict[str, list[str]] = {
    "read-only": [
        "read_file", "grep_search", "glob_search", "list_directory",
        "web_search", "web_extract", "calculator", "current_datetime",
        "tool_manifest", "config", "clarify",
        # ``tool.task`` is *delegation*, not a tool — it's always allowed.
        # The inner tool set tool-agent then exposes to its ReAct loop is
        # what's actually mode-gated (see ``tool_executor.tools_for_mode``).
        # Without this entry, read-only users could never reach tool-agent at
        # all, which would force every read-only request through the planner
        # LLM's prose path — the planner can't even use ``read_file``.
        "tool.task",
    ],
    "workspace-write": [
        "read_file", "grep_search", "glob_search", "list_directory",
        "web_search", "web_extract", "calculator", "current_datetime",
        "tool_manifest", "config", "clarify",
        "write_file", "edit_file", "apply_patch", "memory", "todo_write",
        "tool.task",
    ],
    "danger-full-access": ["*"],
}


# Tools tool-agent's ReAct loop is allowed to invoke under each mode. Used by
# ``agents.tool_agent.tool_executor.tools_for_mode`` to filter the
# LangChain tool set BEFORE handing it to ``create_react_agent``.
#
# Wider than ``_MODE_WHITELIST`` for two reasons:
#   1. ``run_python`` / ``run_command`` are not directly dispatchable by the
#      planner (they're in ``_INTERNAL_ONLY``), but the ReAct loop legitimately
#      needs them under workspace-write+ to install pip packages or extract
#      binary file formats.
#   2. ``clarify`` is internal-only at the planner level (would hang the
#      synchronous MCP path) but is exactly the tool a ReAct loop wants when
#      it hits ambiguity mid-task.
#
# Read-only stays strict: only the tools that don't mutate disk state or
# execute foreign code. This is what makes ``read-only`` actually mean
# read-only when a user delegates "保存到 a.txt" — tool-agent simply doesn't
# have ``write_file`` bound, so the model can't call it.
_TOOL_AGENT_MODE_TOOLS: dict[str, list[str]] = {
    "read-only": [
        "read_file", "grep_search", "glob_search", "list_directory",
        "web_search", "web_extract", "web_crawl", "clarify",
    ],
    "workspace-write": [
        "read_file", "grep_search", "glob_search", "list_directory",
        "web_search", "web_extract", "web_crawl", "clarify",
        "write_file", "run_python", "run_command",
    ],
    "danger-full-access": ["*"],
}


# Inner whitelist used by skill-agent's ``_mint_tool_grant`` when a skill
# wants to call a tool on the peer tool-agent.
#
# Why this is *more permissive* than ``_MODE_WHITELIST``:
#
# The outer ``_MODE_WHITELIST`` governs what the planner can dispatch
# DIRECTLY from a user message. Adding ``run_command`` there would let
# the planner shell out on its own under workspace-write — that's NOT
# what the user opted into.
#
# Skills are different: the user explicitly invoked a curated workflow
# (planner routed ``skill.baidu-ecommerce-search`` to skill-agent), and
# the workflow's whole point is to call domain scripts (``python
# skills/<slug>/scripts/<script>.py``). Forcing every such skill into
# ``danger-full-access`` would make skills practically unusable for the
# common case.
#
# Design:
#  - ``read-only``        — skills don't run at all (outer gate blocks
#                           ``skill.*`` upstream of this map).
#  - ``workspace-write``  — skills get the full toolbox. The user
#                           already trusted the agent with disk writes;
#                           letting a vetted skill call run_command on
#                           a script that ships in the user's own
#                           ``skills/`` directory is consistent with
#                           that trust boundary.
#  - ``danger-full-access`` — same, everything goes.
#
# Defense for skills/<slug>/ itself is "don't drop random SKILL.md in
# your workspace", the same trust model as "don't pip-install random
# packages". Skills are user-installed code.
_SKILL_INNER_WHITELIST: dict[str, list[str]] = {
    "read-only": [],
    "workspace-write": ["*"],
    "danger-full-access": ["*"],
}
