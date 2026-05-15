import json
import os
import subprocess
import sys
from pathlib import Path
import pytest


@pytest.mark.e2e
def test_orchestrator_writes_peers_json_with_both_specialist_urls(tmp_path):
    """End-to-end smoke: after orchestrator runs, peers.json should list both specialists' A2A URLs."""
    target = tmp_path / "peer.txt"
    target.write_text("peer-call works", encoding="utf-8")

    env = os.environ.copy()
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"

    proc = subprocess.run(
        [sys.executable, "cli.py", "prompt", f"read_file:{target}"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr

    peers_path = Path(".agent/runtime/peers.json")
    assert peers_path.exists(), "orchestrator did not write peers.json"
    peers = json.loads(peers_path.read_text(encoding="utf-8"))
    assert "tool-agent" in peers
    assert "skill-agent" in peers
    # URLs are http://127.0.0.1:<port>
    assert peers["tool-agent"].startswith("http://127.0.0.1:")
    assert peers["skill-agent"].startswith("http://127.0.0.1:")
