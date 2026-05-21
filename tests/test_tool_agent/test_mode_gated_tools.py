"""Mode-gated tool exposure on the tool-agent ReAct surface.

Plan-A security fix: when the orchestrator delegates ``tool.task`` to
tool-agent, the recently-added authz_grant carries the user's permission
mode. ``tools_for_mode`` / ``make_langchain_tools(mode=...)`` filter the
LangChain tool set so a read-only delegation literally cannot bind
``write_file`` / ``run_command`` to the model.

These tests pin that surface — without them, the previous bypass
(read-only mode + ReAct loop with full tool set) could quietly regress.
"""
from __future__ import annotations

from agents.tool_agent.tool_executor import make_langchain_tools, tools_for_mode


def test_read_only_excludes_write_and_shell():
    names = set(tools_for_mode("read-only"))
    # Read-class only.
    assert "read_file" in names
    assert "grep_search" in names
    assert "web_search" in names
    assert "clarify" in names
    # Anything that mutates state or executes foreign code must NOT appear.
    for forbidden in ("write_file", "run_command", "run_python"):
        assert forbidden not in names, (
            f"{forbidden!r} leaked into read-only toolset: {sorted(names)}"
        )


def test_workspace_write_adds_write_and_shell():
    names = set(tools_for_mode("workspace-write"))
    # All the read-class tools survive.
    assert "read_file" in names
    assert "grep_search" in names
    # Plus the workspace-mutation + shell tools.
    for added in ("write_file", "run_command", "run_python"):
        assert added in names, (
            f"workspace-write missing {added!r}: {sorted(names)}"
        )


def test_danger_full_access_exposes_everything():
    names = set(tools_for_mode("danger-full-access"))
    # Mode * means: every key in _TOOL_MAP.
    assert "write_file" in names
    assert "run_command" in names
    assert "run_python" in names
    assert "clarify" in names


def test_unknown_mode_falls_back_to_empty():
    """A typo/unknown mode must NOT escalate. Empty set is the safe default —
    the ReAct loop will refuse to use any tools and the user gets a clear
    diagnostic rather than silent over-permission."""
    assert tools_for_mode("super-mega-admin") == []


def test_make_langchain_tools_respects_mode():
    """The structured-tool factory must honor the mode arg, not silently
    return everything."""
    read_only_tools = make_langchain_tools(mode="read-only")
    names = {t.name for t in read_only_tools}
    assert "write_file" not in names
    assert "run_command" not in names
    assert "read_file" in names

    full_tools = make_langchain_tools(mode="danger-full-access")
    full_names = {t.name for t in full_tools}
    assert "write_file" in full_names
    assert "run_command" in full_names


def test_make_langchain_tools_defaults_to_full_access():
    """Backward compatibility: unit tests / single-agent loop construct the
    tool list without a mode kwarg. The default must NOT be ``read-only``
    or those callers regress."""
    default_tools = make_langchain_tools()
    names = {t.name for t in default_tools}
    assert "write_file" in names
    assert "run_command" in names


def test_every_tool_map_key_is_reachable_in_some_mode():
    """Maintenance trap: adding a new tool to ``_TOOL_MAP`` without also
    updating ``_TOOL_AGENT_MODE_TOOLS`` silently makes it unreachable
    except in ``danger-full-access``. This test fails loudly so the author
    has to make a real decision about which modes the tool belongs to.

    Acceptable resolutions:
      * Add the tool to the right mode(s) in ``_TOOL_AGENT_MODE_TOOLS``.
      * If the tool is genuinely danger-only, leave it out of read-only /
        workspace-write but add an explicit comment to ``_TOOL_AGENT_MODE_TOOLS``
        documenting that choice — and update this test to skip the tool by
        name (the test is the maintainer's checklist).
    """
    from agents.tool_agent.tool_executor import _TOOL_MAP

    union: set[str] = set()
    for mode in ("read-only", "workspace-write", "danger-full-access"):
        union.update(tools_for_mode(mode))

    missing = set(_TOOL_MAP.keys()) - union
    assert not missing, (
        f"Tools registered in _TOOL_MAP but not bound in any permission "
        f"mode: {sorted(missing)}. Add them to _TOOL_AGENT_MODE_TOOLS in "
        f"agents/shared/permission_modes.py."
    )


def test_mode_whitelist_and_tool_agent_mode_tools_have_same_keys():
    """The two mode tables must stay in lockstep.

    ``_MODE_WHITELIST`` governs what the planner can dispatch (outer gate)
    and ``_TOOL_AGENT_MODE_TOOLS`` governs what the ReAct loop has bound
    (inner gate). If a new permission mode is added to one without the
    other, ``handle_tool_task_stream`` validates the claim against
    ``_MODE_WHITELIST`` (passes) and then ``tools_for_mode`` returns an
    empty list (silent mute agent). Asserting parity at test time prevents
    the bug from shipping.
    """
    from agents.shared.permission_modes import (
        _MODE_WHITELIST,
        _TOOL_AGENT_MODE_TOOLS,
    )

    outer = set(_MODE_WHITELIST.keys())
    inner = set(_TOOL_AGENT_MODE_TOOLS.keys())
    assert outer == inner, (
        f"Permission-mode tables out of sync:\n"
        f"  _MODE_WHITELIST has: {sorted(outer)}\n"
        f"  _TOOL_AGENT_MODE_TOOLS has: {sorted(inner)}\n"
        f"  only in outer (planner OK, but ReAct gets empty toolset): "
        f"{sorted(outer - inner)}\n"
        f"  only in inner (ReAct ready but planner can't dispatch): "
        f"{sorted(inner - outer)}"
    )
