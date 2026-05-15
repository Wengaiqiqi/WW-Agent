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
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"

    # Phase-5 stub planner parses 'CAPABILITY:ARG'
    prompt = f"read_file:{target}"

    proc = subprocess.run(
        [sys.executable, "cli.py", "prompt", prompt],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "[tool]" in proc.stdout
    assert "hi there" in proc.stdout
