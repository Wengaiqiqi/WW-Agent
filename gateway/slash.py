"""Slash commands for chat-platform gateways (QQ / Feishu).

Whitelisted users drive remote A2A peers from chat:
  /task <peer_id> <task>     one-shot delegation (comm.delegate)
  /chat <peer_id> <message>  multi-turn conversation (comm.chat, context kept)
  /peers                     list registered peer_ids
  /help                      usage

``handle_slash`` returns the reply STRING for a handled command, or ``None`` to
fall through to the normal planner path (non-slash input, or an unrecognized
/command). UI-free on purpose: the REPL's ReplCommandHandler is coupled to Rich
rendering and an in-memory current-peer; the gateway runs one isolated turn per
message and needs a plain-text reply.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gateway import credentials as gw_creds

COMM_AGENT_ID = "comm-agent"
_RECOGNIZED = {"/task", "/chat", "/peers", "/help"}


def _platform_from_session_key(session_key: str) -> str:
    """``qq:123`` -> ``qq``; ``feishu:abc`` -> ``feishu``; no prefix -> ``""``."""
    if not session_key or ":" not in session_key:
        return ""
    return session_key.split(":", 1)[0]


def _allowed_users(platform: str) -> list[str]:
    """Read the per-platform allowlist from gateways.json.

    Accepts either a comma-separated string (what the setup wizard writes) or a
    JSON list (a hand-edited gateways.json). Empty / missing -> ``[]``.
    """
    if not platform:
        return []
    users = gw_creds.load(platform).get("allowed_users") or []
    if isinstance(users, str):
        users = [u.strip() for u in users.split(",") if u.strip()]
    return [str(u) for u in users]


def _is_authorized(session_key: str, user_id: str) -> bool:
    """Fail-safe: no user id, or empty/missing allowlist, denies."""
    if not user_id:
        return False
    return user_id in _allowed_users(_platform_from_session_key(session_key))
