from __future__ import annotations
import time
import jwt as pyjwt


class PermissionDenied(Exception):
    pass


_MODE_WHITELIST: dict[str, list[str]] = {
    "read-only": [
        "read_file", "grep_search", "glob_search", "list_directory",
        "web_search", "web_extract", "calculator", "current_datetime",
        "tool_manifest", "config", "clarify",
    ],
    "workspace-write": [
        "read_file", "grep_search", "glob_search", "list_directory",
        "web_search", "web_extract", "calculator", "current_datetime",
        "tool_manifest", "config", "clarify",
        "write_file", "edit_file", "apply_patch", "memory", "todo_write",
    ],
    "danger-full-access": ["*"],
}


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
        return "*" in wl or tool in wl

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
