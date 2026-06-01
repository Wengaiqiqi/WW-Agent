"""The explicit per-turn context that replaces process-global os.environ state.

Built once at each entry point (CLI / gateway / web) — the ONLY place env or
request data is read into the orchestrator. Everything downstream takes the
context (or a config resolved from it) explicitly, so two turns running
concurrently never share mutable global state.

``turn_env()`` is the cross-process channel: the dict handed to MCPHost and
merged into each spawned specialist's environment. The parent process's
``os.environ`` is never mutated.
"""
from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TurnContext:
    turn_id: str
    user_id: str
    workspace_root: Path
    permission_mode: str
    model_id: str
    base_url: str
    api_key: str
    protocol: str
    session_key: str
    trace_id: str
    hmac_key: str
    runtime_dir: Path

    def turn_env(self) -> dict[str, str]:
        """The per-turn env overlay handed to spawned subprocesses. Empty
        optionals are omitted so a child default isn't clobbered with ""; the
        always-present vars (permission mode, workspace, runtime dir) are set
        unconditionally."""
        env: dict[str, str] = {
            "LANGCHAIN_AGENT_PERMISSION_MODE": self.permission_mode,
            "LANGCHAIN_AGENT_WORKSPACE_ROOT": str(self.workspace_root),
            "LANGCHAIN_AGENT_RUNTIME_DIR": str(self.runtime_dir),
        }
        optionals = {
            "LANGCHAIN_AGENT_MEMORY_USER": self.user_id,
            "LANGCHAIN_AGENT_MODEL": self.model_id,
            "LANGCHAIN_AGENT_BASE_URL": self.base_url,
            "LANGCHAIN_AGENT_API_KEY": self.api_key,
            "LANGCHAIN_AGENT_PROTOCOL": self.protocol,
        }
        env.update({k: v for k, v in optionals.items() if v})
        return env

    @classmethod
    def from_env(
        cls,
        *,
        session_key: str = "",
        trace_id: str = "",
        hmac_key: str = "",
        runtime_dir: Path | None = None,
        workspace_root: Path | None = None,
    ) -> "TurnContext":
        """Build a context from the current process env — the single-user CLI /
        legacy path. Reproduces today's env-based behavior so existing surfaces
        are unaffected."""
        return cls(
            turn_id=uuid.uuid4().hex,
            user_id=os.environ.get("LANGCHAIN_AGENT_MEMORY_USER", ""),
            workspace_root=workspace_root or Path(
                os.environ.get("LANGCHAIN_AGENT_WORKSPACE_ROOT", "") or os.getcwd()
            ),
            permission_mode=os.environ.get(
                "LANGCHAIN_AGENT_PERMISSION_MODE", "danger-full-access"
            ),
            model_id=os.environ.get("LANGCHAIN_AGENT_MODEL", ""),
            base_url=os.environ.get("LANGCHAIN_AGENT_BASE_URL", ""),
            api_key=os.environ.get("LANGCHAIN_AGENT_API_KEY", ""),
            protocol=os.environ.get("LANGCHAIN_AGENT_PROTOCOL", ""),
            session_key=session_key,
            trace_id=trace_id or uuid.uuid4().hex[:8],
            hmac_key=hmac_key or secrets.token_urlsafe(32),
            runtime_dir=runtime_dir or Path(".agent") / "runtime" / "cli",
        )
