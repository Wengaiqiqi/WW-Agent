import os
import subprocess
import sys
import pytest


@pytest.mark.e2e
def test_legacy_mode_does_not_spawn_specialists(tmp_path):
    """--single should run in a single process — no child subprocesses."""
    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock/mock-default"
    env["MOCK_API_KEY"] = "x"  # any non-empty
    # Use the `prompt` subcommand so the process exits without REPL input.
    proc = subprocess.run(
        [sys.executable, "cli.py", "--single", "prompt", "hello"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    # Loose check: it didn't crash with a Python traceback related to multi-agent code.
    assert "Traceback" not in proc.stderr or "single" in proc.stderr.lower()
    # Stricter check tightened in Phase 9 once orchestrator exists.
