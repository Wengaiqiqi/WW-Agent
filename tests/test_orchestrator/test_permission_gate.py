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
