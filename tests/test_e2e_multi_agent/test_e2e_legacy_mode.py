# tests/test_e2e_multi_agent/test_e2e_legacy_mode.py
import os
import subprocess
import sys
import psutil
import pytest


@pytest.mark.e2e
def test_legacy_mode_does_not_spawn_specialists(tmp_path):
    """python cli.py --single must NOT spawn agents.tool_agent or agents.skill_agent."""
    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock/mock-default"
    env["MOCK_API_KEY"] = "x"

    proc = subprocess.Popen(
        [sys.executable, "cli.py", "--single", "prompt", "hello"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    seen_specialist = False
    try:
        parent = psutil.Process(proc.pid)
        # Inspect children while the process is alive; loop briefly because the
        # legacy process may exit very fast in mock mode.
        for _ in range(20):  # ~1 second
            try:
                for child in parent.children(recursive=True):
                    try:
                        cmdline = " ".join(child.cmdline())
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        continue
                    if "agents.tool_agent" in cmdline or "agents.skill_agent" in cmdline:
                        seen_specialist = True
                        break
                if seen_specialist or proc.poll() is not None:
                    break
            except psutil.NoSuchProcess:
                break
            import time
            time.sleep(0.05)
    finally:
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

    assert not seen_specialist, "legacy --single mode must not spawn specialist subprocesses"
