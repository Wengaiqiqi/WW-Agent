"""SQLite-backed store for web accounts, conversations, and messages.

Pure data layer: no HTTP, no orchestrator imports. Every function takes an
explicit ``db_path`` and opens a short-lived connection (sqlite connects are
cheap; this keeps the module thread-safe under FastAPI's mixed sync/async
handlers without a shared connection + lock).
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


class DuplicateUsername(Exception):
    """Raised when registering a username that already exists."""


def default_db_path() -> str:
    from agent_paths import config_dir

    return str(config_dir() / "web" / "app.db")


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _new_id() -> str:
    return secrets.token_hex(16)


def _now() -> int:
    return int(time.time())


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                pwd_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                events_json TEXT NOT NULL DEFAULT '[]',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);
            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id);
            """
        )


def create_user(db_path: str, username: str, pwd_hash: str, salt: str) -> str:
    uid = _new_id()
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO users (id, username, pwd_hash, salt, role, created_at) "
                "VALUES (?, ?, ?, ?, 'user', ?)",
                (uid, username, pwd_hash, salt, _now()),
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateUsername(username) from exc
    return uid


def get_user_by_username(db_path: str, username: str) -> Optional[dict[str, Any]]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_user(db_path: str, user_id: str) -> Optional[dict[str, Any]]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None
