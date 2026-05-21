# tests/test_e2e_multi_agent/test_e2e_simple_tool.py
import os
import subprocess
import sys
import pytest


@pytest.mark.e2e
def test_orchestrator_dispatches_read_file_to_tool_agent(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hi there", encoding="utf-8")

    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock"
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"
    env["PYTHONIOENCODING"] = "utf-8"
    # ``_wrap_read_file`` now enforces the workspace boundary; widen it to the
    # tmp_path so the e2e test's fixture file is in-scope.
    env["LANGCHAIN_AGENT_WORKSPACE_ROOT"] = str(tmp_path)

    # Phase-5 stub planner parses 'CAPABILITY:ARG'
    prompt = f"read_file:{target}"

    proc = subprocess.run(
        [sys.executable, "cli.py", "prompt", prompt],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "[tool]" in proc.stdout
    assert "hi there" in proc.stdout


@pytest.mark.e2e
def test_multi_agent_repl_dispatches_turn_and_exits(tmp_path):
    target = tmp_path / "hello-repl.txt"
    target.write_text("hello from repl", encoding="utf-8")

    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock"
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"
    env["PYTHONIOENCODING"] = "utf-8"
    env["LANGCHAIN_AGENT_WORKSPACE_ROOT"] = str(tmp_path)

    proc = subprocess.run(
        [sys.executable, "cli.py"],
        input=f"read_file:{target}\n/exit\n",
        capture_output=True,
        text=True, encoding="utf-8",
        env=env,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert "multi-agent REPL not fully implemented" not in proc.stdout
    assert "hello from repl" in proc.stdout
    # Windows anyio stdio_client cleanup may produce tracebacks during
    # asyncio.run() shutdown that cannot be suppressed from application code.
    # These are harmless and known — don't assert stderr cleanliness here.
