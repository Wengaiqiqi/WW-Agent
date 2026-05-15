from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VALID_PERMISSION_MODES = {"read-only", "workspace-write", "danger-full-access"}
DEFAULT_PERMISSION_MODE = "workspace-write"
MAX_HISTORY_ITEMS = 12


@dataclass
class MultiAgentSessionState:
    provider: str
    model: str
    protocol: str
    base_url: str
    api_key_env: str
    permission_mode: str
    workspace: Path
    thread_id: str = "multi-agent-session-1"
    turns: int = 0
    tool_calls: int = 0
    compacted_turns: int = 0
    seen_messages: int = 0
    last_error: str | None = None
    recent_history: list[dict[str, Any]] = field(default_factory=list)
    memory_snapshot: str = ""
    instruction_files: list[Any] = field(default_factory=list)
    skills: list[Any] = field(default_factory=list)

    @classmethod
    def from_runtime(
        cls,
        *,
        active_cfg,
        skills: list[Any],
        instruction_files: list[Any],
        memory_snapshot: str,
        workspace: Path,
    ) -> "MultiAgentSessionState":
        permission_mode = os.environ.get("LANGCHAIN_AGENT_PERMISSION_MODE", DEFAULT_PERMISSION_MODE)
        if permission_mode not in VALID_PERMISSION_MODES:
            permission_mode = DEFAULT_PERMISSION_MODE
        os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] = permission_mode
        return cls(
            provider=active_cfg.provider,
            model=active_cfg.model,
            protocol=active_cfg.protocol,
            base_url=active_cfg.base_url,
            api_key_env=active_cfg.api_key_env,
            permission_mode=permission_mode,
            workspace=workspace,
            memory_snapshot=memory_snapshot,
            instruction_files=list(instruction_files),
            skills=list(skills),
        )

    def set_permission_mode(self, mode: str) -> bool:
        if mode not in VALID_PERMISSION_MODES:
            return False
        self.permission_mode = mode
        os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] = mode
        return True

    def apply_config(self, cfg) -> None:
        self.provider = cfg.provider
        self.model = cfg.model
        self.protocol = cfg.protocol
        self.base_url = cfg.base_url
        self.api_key_env = cfg.api_key_env

    def record_turn(
        self,
        *,
        user_input: str,
        capability: str,
        owner: str,
        observation: str,
        error: str | None,
    ) -> None:
        self.turns += 1
        self.seen_messages += 1
        self.last_error = error
        if capability:
            self.tool_calls += 1
        self.recent_history.append(
            {
                "user": user_input,
                "capability": capability,
                "owner": owner,
                "observation": observation,
                "error": error,
            }
        )
        if len(self.recent_history) > MAX_HISTORY_ITEMS:
            self.recent_history = self.recent_history[-MAX_HISTORY_ITEMS:]

    def compact(self, *, memory_snapshot: str) -> None:
        self.compacted_turns += self.turns
        self.turns = 0
        self.seen_messages = 0
        self.last_error = None
        self.recent_history.clear()
        self.memory_snapshot = memory_snapshot
        suffix = self.compacted_turns + 1
        self.thread_id = f"multi-agent-session-{suffix}"
