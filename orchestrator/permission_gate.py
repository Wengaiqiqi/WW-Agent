from __future__ import annotations
import time
import jwt as pyjwt

# Re-export from the shared module so existing callers (and tests) that do
# ``from orchestrator.permission_gate import PermissionDenied, _MODE_WHITELIST``
# keep working unchanged. The single source of truth lives in
# ``agents.shared.permission_modes`` so skill-agent can read the same table
# without a reverse-direction import.
from agents.shared.permission_modes import PermissionDenied, _MODE_WHITELIST  # noqa: F401


class PermissionGate:
    """Decides whether a tool may be called under the current mode and signs
    a short-lived authz_grant JWT for the chosen specialist."""

    def __init__(self, *, mode: str, hmac_key: str, trace_id: str):
        if mode not in _MODE_WHITELIST:
            raise ValueError(f"unknown permission mode: {mode}")
        self.mode = mode
        self.hmac_key = hmac_key
        self.trace_id = trace_id

    def _is_allowed(self, tool: str) -> bool:
        wl = _MODE_WHITELIST[self.mode]
        if "*" in wl:
            return True
        if tool in wl:
            return True
        # skill.* dispatches are themselves opaque — a skill can internally
        # invoke any tool-agent capability via _mint_tool_grant. Anyone who
        # can drop a SKILL.md under skills/ would otherwise side-step the
        # outer mode. Allow skills only when the user explicitly opted into
        # at least workspace-write; under read-only, the safest default is
        # "no skill execution" — most skills perform side effects anyway.
        # skill-agent's _mint_tool_grant re-validates the inner tool against
        # the inherited mode, so a skill running here still can't reach
        # run_command unless the user is in danger-full-access.
        if tool.startswith("skill.") and self.mode != "read-only":
            return True
        return False

    def sign(self, *, target_specialist: str, tool: str) -> str:
        if not self._is_allowed(tool):
            raise PermissionDenied(
                f"tool {tool!r} not permitted under mode {self.mode!r}"
            )
        now = int(time.time())
        payload = {
            "iss": "orchestrator",
            "sub": target_specialist,
            "exp": now + 60,
            "permission_mode": self.mode,
            "allowed_tools": [tool],
            "trace_id": self.trace_id,
        }
        return pyjwt.encode(payload, self.hmac_key, algorithm="HS256")
