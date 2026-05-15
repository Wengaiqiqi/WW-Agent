from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path


class PermissionMode(IntEnum):
    READ_ONLY = 1
    WORKSPACE_WRITE = 2
    DANGER_FULL_ACCESS = 3

    @classmethod
    def parse(cls, value: str | None) -> "PermissionMode":
        if value is None:
            return cls.WORKSPACE_WRITE
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"read-only", "readonly", "read"}:
            return cls.READ_ONLY
        if normalized in {"workspace-write", "workspace", "write"}:
            return cls.WORKSPACE_WRITE
        if normalized in {"danger-full-access", "danger", "full", "allow"}:
            return cls.DANGER_FULL_ACCESS
        import logging
        logging.getLogger(__name__).warning(
            "Unknown permission mode %r, defaulting to read-only", value
        )
        return cls.READ_ONLY

    @property
    def label(self) -> str:
        return {
            self.READ_ONLY: "read-only",
            self.WORKSPACE_WRITE: "workspace-write",
            self.DANGER_FULL_ACCESS: "danger-full-access",
        }[self]


@dataclass(frozen=True)
class PermissionPolicy:
    active_mode: PermissionMode
    tool_requirements: dict[str, PermissionMode] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "PermissionPolicy":
        configured_mode = os.getenv("LANGCHAIN_AGENT_PERMISSION_MODE")
        if configured_mode is None:
            configured_mode = load_local_permission_mode()
        return cls(PermissionMode.parse(configured_mode))

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        return self.tool_requirements.get(tool_name, PermissionMode.DANGER_FULL_ACCESS)

    def authorize(self, tool_name: str, required: PermissionMode, payload: str = "") -> None:
        if self.active_mode >= required:
            return
        detail = f" for {payload}" if payload else ""
        raise PermissionError(
            f"Tool '{tool_name}' requires {required.label} permission{detail}; "
            f"current mode is {self.active_mode.label}. "
            "Set LANGCHAIN_AGENT_PERMISSION_MODE if you intentionally want to allow it."
        )


def authorize_tool(tool_name: str, required: PermissionMode, payload: str = "") -> None:
    PermissionPolicy.from_env().authorize(tool_name, required, payload)


def load_local_permission_mode() -> str | None:
    import agent_paths

    try:
        settings = json.loads(agent_paths.settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = settings.get("permission.defaultMode")
    return str(value) if value is not None else None
