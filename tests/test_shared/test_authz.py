import time
import jwt as pyjwt
import pytest
from agents.shared.authz import verify_grant, AuthzError

KEY = "test-secret-key"


def _make(payload: dict) -> str:
    return pyjwt.encode(payload, KEY, algorithm="HS256")


def test_valid_grant_allows_listed_tool():
    token = _make({
        "iss": "orchestrator",
        "sub": "tool-agent",
        "exp": int(time.time()) + 60,
        "permission_mode": "workspace-write",
        "allowed_tools": ["read_file", "grep_search"],
        "trace_id": "t1",
    })
    claims = verify_grant(token, key=KEY, requested_tool="read_file")
    assert claims["sub"] == "tool-agent"
    assert claims["trace_id"] == "t1"


def test_expired_grant_rejected():
    token = _make({
        "iss": "orchestrator",
        "sub": "tool-agent",
        "exp": int(time.time()) - 1,
        "permission_mode": "read-only",
        "allowed_tools": ["read_file"],
        "trace_id": "t1",
    })
    with pytest.raises(AuthzError, match="expired"):
        verify_grant(token, key=KEY, requested_tool="read_file")


def test_tampered_signature_rejected():
    token = _make({
        "iss": "orchestrator", "sub": "tool-agent",
        "exp": int(time.time()) + 60,
        "permission_mode": "read-only", "allowed_tools": ["read_file"],
        "trace_id": "t1",
    })
    bad = token[:-4] + "AAAA"
    with pytest.raises(AuthzError, match="signature"):
        verify_grant(bad, key=KEY, requested_tool="read_file")


def test_off_whitelist_tool_rejected():
    token = _make({
        "iss": "orchestrator", "sub": "tool-agent",
        "exp": int(time.time()) + 60,
        "permission_mode": "workspace-write",
        "allowed_tools": ["read_file"],
        "trace_id": "t1",
    })
    with pytest.raises(AuthzError, match="not in allowed_tools"):
        verify_grant(token, key=KEY, requested_tool="run_command")
