import time
import pytest
from orchestrator.permission_gate import PermissionGate, PermissionDenied
from agents.shared.authz import verify_grant


def test_read_only_mode_allows_read_tools():
    gate = PermissionGate(mode="read-only", hmac_key="k", trace_id="t1")
    grant = gate.sign(target_specialist="tool-agent", tool="read_file")
    claims = verify_grant(grant, key="k", requested_tool="read_file")
    assert claims["permission_mode"] == "read-only"
    assert claims["sub"] == "tool-agent"
    assert claims["trace_id"] == "t1"


def test_read_only_mode_denies_write_tools():
    gate = PermissionGate(mode="read-only", hmac_key="k", trace_id="t1")
    with pytest.raises(PermissionDenied, match="write_file"):
        gate.sign(target_specialist="tool-agent", tool="write_file")


def test_workspace_write_allows_writes_denies_shell():
    gate = PermissionGate(mode="workspace-write", hmac_key="k", trace_id="t1")
    gate.sign(target_specialist="tool-agent", tool="write_file")  # ok
    with pytest.raises(PermissionDenied, match="run_command"):
        gate.sign(target_specialist="tool-agent", tool="run_command")


def test_danger_full_access_allows_shell():
    gate = PermissionGate(mode="danger-full-access", hmac_key="k", trace_id="t1")
    gate.sign(target_specialist="tool-agent", tool="run_command")  # ok


def test_grant_expires_in_60_seconds():
    gate = PermissionGate(mode="read-only", hmac_key="k", trace_id="t1")
    grant = gate.sign(target_specialist="tool-agent", tool="read_file")
    import jwt as pyjwt
    claims = pyjwt.decode(grant, "k", algorithms=["HS256"])
    assert 55 <= claims["exp"] - int(time.time()) <= 60


def test_skill_blocked_in_read_only_mode():
    """Regression: skills used to be waved through under any mode. Now they
    require at least workspace-write — anyone with write access to
    ``skills/*/SKILL.md`` would otherwise side-step the user's mode."""
    gate = PermissionGate(mode="read-only", hmac_key="k", trace_id="t1")
    with pytest.raises(PermissionDenied, match="skill"):
        gate.sign(target_specialist="skill-agent", tool="skill.some-skill")


def test_skill_allowed_in_workspace_write_mode():
    gate = PermissionGate(mode="workspace-write", hmac_key="k", trace_id="t1")
    # Should not raise.
    gate.sign(target_specialist="skill-agent", tool="skill.baidu-ecommerce-search")


def test_skill_allowed_in_danger_mode():
    gate = PermissionGate(mode="danger-full-access", hmac_key="k", trace_id="t1")
    gate.sign(target_specialist="skill-agent", tool="skill.anything")


def test_skill_cannot_mint_grant_for_disallowed_tool():
    """A skill running under workspace-write must NOT be able to mint an inner
    grant for run_command — escalation via _mint_tool_grant is the realistic
    attack path that the gate's outer skill.* allowance previously enabled."""
    from agents.skill_agent.skill_executor import _mint_tool_grant
    import os
    os.environ["AUTHZ_HMAC_KEY"] = "k"
    with pytest.raises(PermissionDenied, match="run_command"):
        _mint_tool_grant("run_command", {"permission_mode": "workspace-write"})


def test_skill_can_mint_grant_for_allowed_tool():
    """workspace-write permits write_file, so a skill running under it should be
    able to mint a sub-grant for write_file on tool-agent."""
    from agents.skill_agent.skill_executor import _mint_tool_grant
    import os
    os.environ["AUTHZ_HMAC_KEY"] = "k"
    # Should not raise.
    _mint_tool_grant("write_file", {"permission_mode": "workspace-write"})
