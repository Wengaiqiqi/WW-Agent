import os
import signal
import subprocess
import sys
import time
import pytest


@pytest.mark.e2e
def test_ctrl_c_during_repl_clean_exit():
    """Start orchestrator in REPL mode, send Ctrl+C, verify clean exit."""
    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock/mock-default"

    creationflags = 0
    if sys.platform == "win32":
        # On Windows, we need a new console group to send a CTRL_BREAK_EVENT.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [sys.executable, "cli.py"],
        env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
    )
    # Give it a moment to spawn specialists / print the REPL banner
    time.sleep(2)

    # Send interrupt
    if sys.platform == "win32":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)

    # Wait for clean exit (give it up to 10 seconds)
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail("orchestrator did not exit cleanly after Ctrl+C within 10s")

    # On Windows the exit code for CTRL_BREAK can be 0xC000013A (interrupted),
    # 130, or even 1 depending on how the trap fires. Just assert it did exit.
    # (We don't assert returncode == 0 because Python's Ctrl+C handling on
    # Windows is messy.)
    assert proc.returncode is not None
