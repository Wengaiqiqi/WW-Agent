from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VALID_PERMISSION_MODES = {"read-only", "workspace-write", "danger-full-access"}
DEFAULT_PERMISSION_MODE = "workspace-write"
MAX_HISTORY_ITEMS = 12

# Bytes budgets for the planner context block — protect the system prompt
# from runaway growth as the session accumulates large tool observations.
_MAX_OBSERVATION_CHARS = 600
_RECENT_HISTORY_FULL = 3
_MAX_MEMORY_CHARS = 4000
_MAX_INSTRUCTION_CHARS = 6000


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
        active_cfg: Any,
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

    def apply_config(self, cfg: Any) -> None:
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

    def render_planner_context(self, capabilities: list[str]) -> str:
        # Instructions — bounded by a shared budget across all files.
        instruction_sections: list[str] = []
        remaining_instr = _MAX_INSTRUCTION_CHARS
        for file in self.instruction_files:
            if remaining_instr <= 0:
                instruction_sections.append("…[more instruction files omitted]")
                break
            path = getattr(file, "path", "")
            content = getattr(file, "content", "")
            if not content:
                continue
            if len(content) > remaining_instr:
                content = content[:remaining_instr] + "\n…[truncated]"
            remaining_instr -= len(content)
            instruction_sections.append(f"## {path}\n{content}")

        skill_lines = []
        for skill in self.skills:
            name = getattr(skill, "name", "")
            title = getattr(skill, "title", "")
            skill_lines.append(f"- {name}: {title}")

        # Recent history — keep the most recent _RECENT_HISTORY_FULL items
        # with full observations, older items collapse to capability-only so
        # earlier turns don't bloat the system prompt indefinitely.
        history_lines: list[str] = []
        total = len(self.recent_history)
        cutoff = total - _RECENT_HISTORY_FULL
        for idx, item in enumerate(self.recent_history):
            if idx < cutoff:
                history_lines.append(
                    f"User: {item['user']}\nCapability: {item['capability']} (older — observation elided)"
                )
                continue
            observation = item.get("observation") or ""
            if len(observation) > _MAX_OBSERVATION_CHARS:
                observation = observation[:_MAX_OBSERVATION_CHARS] + "\n…[truncated]"
            parts = [
                f"User: {item['user']}",
                f"Capability: {item['capability']}",
                f"Owner: {item['owner']}",
                f"Observation: {observation}",
            ]
            if item.get("error"):
                parts.append(f"Error: {item['error']}")
            history_lines.append("\n".join(parts))

        # Memory snapshot can also accrete over time; cap it so a long
        # MEMORY.md doesn't crowd out everything else.
        memory = self.memory_snapshot or ""
        if len(memory) > _MAX_MEMORY_CHARS:
            memory = memory[:_MAX_MEMORY_CHARS] + "\n…[truncated; full memory available via the memory tool]"

        return "\n\n".join([
            f"Provider: {self.provider}",
            f"Model: {self.model}",
            f"Protocol: {self.protocol}",
            f"Permission mode: {self.permission_mode}",
            "Capabilities:\n" + "\n".join(f"- {c}" for c in capabilities),
            "Memory:\n" + (memory or "<none>"),
            "Project instructions:\n" + ("\n\n".join(instruction_sections) or "<none>"),
            "Skills:\n" + ("\n".join(skill_lines) or "<none>"),
            "Recent history:\n" + ("\n\n".join(history_lines) or "<none>"),
        ])
