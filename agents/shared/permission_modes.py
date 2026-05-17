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
    ],
    "workspace-write": [
        "read_file", "grep_search", "glob_search", "list_directory",
        "web_search", "web_extract", "calculator", "current_datetime",
        "tool_manifest", "config", "clarify",
        "write_file", "edit_file", "apply_patch", "memory", "todo_write",
    ],
    "danger-full-access": ["*"],
}
