# tests/test_e2e_multi_agent/test_e2e_skill_a2a_chain.py
import os
import subprocess
import sys
import pytest


@pytest.mark.e2e
def test_skill_chain_via_a2a(tmp_path):
    """End-to-end: orchestrator → skill-agent → A2A → tool-agent → result.

    Uses the mock provider with scripted responses to make the LLM path
    deterministic. Verifies the unified-stream output contains both [skill]
    and [tool] tags (the latter via telemetry from the A2A invocation)."""
    target = tmp_path / "input.txt"
    target.write_text("PAYLOAD-XYZ", encoding="utf-8")

    # The path string needs JSON-escaping for the script.
    import json as _json
    path_json = _json.dumps(str(target))  # produces "..." with escapes

    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock/mock-default"
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"
    env["PYTHONIOENCODING"] = "utf-8"  # child emits UTF-8 (we read it as UTF-8)

    # Orchestrator routes to skill.baidu-ecommerce-search (one of the real
    # registered skills). Skill name doesn't matter — only its presence in
    # the router does.
    env["MOCK_ORCH_SCRIPT"] = (
        '{"capability":"skill.baidu-ecommerce-search","arguments":{}}'
    )

    # First LLM response asks for a read; second produces the final answer.
    env["MOCK_SKILL_SCRIPT"] = (
        '{"tool_calls":[{"tool":"read_file","arguments":{"path":' + path_json + '}}]}'
        '||'
        '{"final":"Got payload PAYLOAD-XYZ"}'
    )

    proc = subprocess.run(
        [sys.executable, "cli.py", "prompt", "do the demo"],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert proc.returncode == 0, (
        f"stderr:\n{proc.stderr}\n---\nstdout:\n{proc.stdout}"
    )
    # [skill] tag should appear (final answer rendered through StreamMux)
    assert "[skill]" in proc.stdout, f"stdout missing [skill]: {proc.stdout!r}"
    # [tool] tag should appear via telemetry from the A2A invocation
    assert "[tool]" in proc.stdout, f"stdout missing [tool]: {proc.stdout!r}"
    # The final answer text comes through
    assert "PAYLOAD-XYZ" in proc.stdout, f"stdout missing payload: {proc.stdout!r}"
