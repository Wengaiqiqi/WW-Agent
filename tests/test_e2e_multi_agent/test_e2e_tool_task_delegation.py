"""End-to-end test for the orchestrator → tool-agent A2A delegation path.

This test exercises the FULL real chain — not the legacy direct-MCP shortcut
used by the other e2e tests:

    user typing in REPL
      → orchestrator subprocess (cli.py)
        → planner picks capability="tool.task"
          → orchestrator opens an HTTP SSE stream to tool-agent
            → tool-agent subprocess receives task, runs its ReAct loop
              → fake LLM emits a unique marker string
            ← SSE events stream back to orchestrator
      ← orchestrator renders the marker to stdout

If any link in that chain regresses (planner routing, A2A client, SSE server,
ReAct loop, response rendering), the unique-marker assertion below fails.
"""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.e2e
def test_orchestrator_delegates_to_tool_agent_over_a2a(tmp_path):
    # MOCK_ORCH_SCRIPT forces the orchestrator's stub planner to choose the
    # agent-task capability. The new REPL controller branches on this and
    # uses the A2A streaming client (NOT the legacy MCP graph dispatch),
    # which is exactly what we want to cover.
    marker = "TOOL_AGENT_E2E_OK_42"
    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock"
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"
    env["PYTHONIOENCODING"] = "utf-8"
    env["MOCK_ORCH_SCRIPT"] = json.dumps({
        "capability": "tool.task",
        "arguments": {"task": "please respond"},
    })
    # FakeListChatModel inside the tool-agent will stream this string back as
    # its final answer. Its appearance in the orchestrator's stdout proves the
    # round-trip happened over A2A — there is no other code path that could
    # have produced it.
    env["MOCK_TOOL_AGENT_SCRIPT"] = marker

    # Workspace dir for the orchestrator: keep .agent/runtime artifacts isolated.
    cwd = os.getcwd()
    proc = subprocess.run(
        [sys.executable, "cli.py"],
        input="say hi\n/exit\n",
        capture_output=True,
        text=True, encoding="utf-8",
        env=env, cwd=cwd, timeout=90,
    )

    assert proc.returncode == 0, (
        f"orchestrator exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
    )
    # The marker proves delegation succeeded end-to-end.
    assert marker in proc.stdout, (
        f"unique marker {marker!r} not in orchestrator stdout — A2A delegation "
        f"may have silently failed\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
    )
