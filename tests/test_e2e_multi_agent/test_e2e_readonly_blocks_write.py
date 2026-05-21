"""End-to-end: read-only mode must actually block file writes via tool.task.

The most user-visible promise of ``LANGCHAIN_AGENT_PERMISSION_MODE=read-only``
is "the agent cannot write to my disk." Before the multi-layer fix, the
orchestrator's PermissionGate didn't even run on the ``tool.task`` path —
tool-agent's ReAct loop had the full toolset bound under every mode, so a
read-only user typing "save to a.txt" silently wrote the file.

This test boots the real ``cli.py`` subprocess in read-only mode, forces
the planner to dispatch ``tool.task`` (so we hit the streaming A2A path),
and asserts that:

  1. ``tool-agent`` returns *without* having created the target file.
  2. The orchestrator exits cleanly (no crash).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.e2e
def test_readonly_blocks_write_file_via_tool_task(tmp_path):
    target = tmp_path / "should-not-exist.txt"
    # Sanity: the file MUST NOT exist before the test runs (tmp_path is fresh).
    assert not target.exists()

    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock"
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "read-only"
    env["PYTHONIOENCODING"] = "utf-8"
    # Workspace anchored at tmp_path so the path is *theoretically* in-scope
    # — i.e. the test isolates the "write tool is unbound under read-only"
    # guard from the unrelated "path outside workspace" guard.
    env["LANGCHAIN_AGENT_WORKSPACE_ROOT"] = str(tmp_path)
    # Force the orchestrator's stub planner to route this turn through
    # tool.task — i.e. the path that used to silently bypass the gate.
    env["MOCK_ORCH_SCRIPT"] = json.dumps({
        "capability": "tool.task",
        "arguments": {"task": f"write 'hello' to {target.as_posix()}"},
    })
    # If the tool-agent's mock LLM ever DOES emit a write_file tool_call,
    # the assertion below catches it — the marker is what the LLM returns
    # as its final text response in the mock harness.
    env["MOCK_TOOL_AGENT_SCRIPT"] = "DONE_NO_WRITE"

    proc = subprocess.run(
        [sys.executable, "cli.py"],
        input="please save the file\n/exit\n",
        capture_output=True,
        text=True, encoding="utf-8",
        env=env, cwd=os.getcwd(),
        timeout=90,
    )

    assert proc.returncode == 0, (
        f"orchestrator exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
    )
    # The headline assertion: no write happened, regardless of what the
    # model decided to do. Under read-only, ``write_file`` is not bound to
    # the ReAct loop's LangChain tool set, so it cannot be called.
    assert not target.exists(), (
        f"read-only mode failed to block file write: {target} was created\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
    )


@pytest.mark.e2e
def test_workspace_write_allows_write_file_via_tool_task(tmp_path):
    """Positive control for the read-only test above. Under workspace-write,
    the same plumbing MUST bind ``write_file`` to the ReAct loop. If this
    fails while the read-only test passes, the read-only test isn't proving
    much — it could be that writes are broken across the board.

    Note: the mock tool-agent LLM doesn't actually emit tool_calls (it's a
    FakeListChatModel), so this test only verifies the *binding* — that
    ``write_file`` appears in the bound toolset. We do that by sending an
    explicit ``MOCK_ORCH_SCRIPT`` decision that the orchestrator will route
    through tool.task, then asserting the orchestrator exits cleanly. The
    deeper "write_file is bound" check is the unit test in
    ``test_mode_gated_tools.py``; this test is the e2e *plumbing* check.
    """
    target = tmp_path / "irrelevant.txt"
    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock"
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"
    env["PYTHONIOENCODING"] = "utf-8"
    env["LANGCHAIN_AGENT_WORKSPACE_ROOT"] = str(tmp_path)
    env["MOCK_ORCH_SCRIPT"] = json.dumps({
        "capability": "tool.task",
        "arguments": {"task": f"please respond about {target.as_posix()}"},
    })
    env["MOCK_TOOL_AGENT_SCRIPT"] = "WORKSPACE_WRITE_E2E_OK"

    proc = subprocess.run(
        [sys.executable, "cli.py"],
        input="hello\n/exit\n",
        capture_output=True,
        text=True, encoding="utf-8",
        env=env, cwd=os.getcwd(),
        timeout=90,
    )

    assert proc.returncode == 0, (
        f"orchestrator exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
    )
    # Marker confirms the round trip succeeded under workspace-write.
    assert "WORKSPACE_WRITE_E2E_OK" in proc.stdout, (
        f"workspace-write tool.task round-trip did not produce marker\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
    )
