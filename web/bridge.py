"""Streaming bridge from the web surface to the orchestrator core.

Mirrors gateway.runner's bootstrap+dispatch but YIELDS events instead of
returning final text only. Reuses runner's helpers and shares its concurrency
guard so a web turn and an in-process gateway turn never run concurrently
(they share .agent/runtime files)."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Iterator

from web import config


def _user_workspace(user_id: str) -> Path:
    from agent_paths import config_dir

    safe = user_id or "anon"
    ws = config_dir() / "web" / "workspaces" / safe
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _set_or_clear(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


@contextlib.contextmanager
def _web_turn_env(*, user_id: str, model_id: str) -> Iterator[Path]:
    """Set the per-turn env (memory user, forced workspace-write, per-user
    workspace root, selected model) and restore the prior values on exit."""
    prev = {
        k: os.environ.get(k)
        for k in (
            "LANGCHAIN_AGENT_MEMORY_USER",
            "LANGCHAIN_AGENT_PERMISSION_MODE",
            "LANGCHAIN_AGENT_WORKSPACE_ROOT",
            "LANGCHAIN_AGENT_MODEL",
        )
    }
    ws = _user_workspace(user_id)
    try:
        _set_or_clear("LANGCHAIN_AGENT_MEMORY_USER", user_id or None)
        _set_or_clear("LANGCHAIN_AGENT_PERMISSION_MODE", config.WEB_PERMISSION_MODE)
        _set_or_clear("LANGCHAIN_AGENT_WORKSPACE_ROOT", str(ws))
        _set_or_clear("LANGCHAIN_AGENT_MODEL", model_id or None)
        yield ws
    finally:
        for k, v in prev.items():
            _set_or_clear(k, v)
