"""Filesystem paths for agent-owned state.

The agent persists settings, credentials, memory, logs, and the live todo list
under a dedicated directory so it does not collide with Claude Code (which
owns ``.claude/``) or other IDE tools.

Default location: ``.langchain-agent/`` relative to the current working
directory. Override with the ``LANGCHAIN_AGENT_CONFIG_DIR`` env var when
you want to put state somewhere else (e.g. ``$XDG_STATE_HOME/...``).
"""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_DIR_NAME = ".langchain-agent"
DEFAULT_RUNTIME_DIR = Path(".agent") / "runtime"


def config_dir() -> Path:
    override = os.getenv("LANGCHAIN_AGENT_CONFIG_DIR", "").strip()
    return Path(override) if override else Path(DEFAULT_DIR_NAME)


def runtime_dir() -> Path:
    """Directory for ephemeral cross-process discovery files: ``peers.json``
    and the ``<agent-id>.a2a-url`` sidecars specialists write at startup.

    Defaults to ``.agent/runtime``. Override with ``LANGCHAIN_AGENT_RUNTIME_DIR``
    so a SECOND orchestrator sharing this process + cwd — notably a chat gateway
    running as a REPL background task — gets an isolated dir and can't clobber
    the REPL's ``peers.json`` / sidecars (or read the REPL's while pointing at
    its own dying subprocesses). Callers are responsible for ``mkdir``.
    """
    override = os.getenv("LANGCHAIN_AGENT_RUNTIME_DIR", "").strip()
    return Path(override) if override else DEFAULT_RUNTIME_DIR


def settings_path() -> Path:
    return config_dir() / "settings.json"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def credentials_gitignore_path() -> Path:
    return config_dir() / ".gitignore"


def memories_dir() -> Path:
    return config_dir() / "memories"


def log_path() -> Path:
    return config_dir() / "agent.log"


def todos_path() -> Path:
    return config_dir() / "todos.json"


def comm_session_path() -> Path:
    """Where the REPL persists its ``/comm use`` selection so the current
    peer survives a restart."""
    return config_dir() / "comm_session.json"
