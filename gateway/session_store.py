"""Per-session conversation history for chat-platform gateways.

A "session" is the tuple ``(platform, chat_id, user_id)`` — same user in the
same chat = same memory. Two different users in the same group chat get
independent histories; the same user's DM and group conversations are also
distinct.

Storage: one JSON file per session under ``<config_dir>/sessions/``. The
filename is the SHA-256 of the session key so we don't have to sanitise
chat IDs / open IDs that may contain characters Windows file systems don't
like. Each file holds the most recent ``HISTORY_TURNS * 2`` messages
(user + assistant counted separately).

Each turn calls :func:`load` to read history and :func:`append` to write the
new pair. The store is process-local: gateway restarts reload the saved JSON,
but two concurrent gateway processes for the same bot would race -- the
``PID lock`` in :mod:`gateway._pidlock` already prevents that.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import List, Tuple

log = logging.getLogger(__name__)


# Default: 25 turns = up to 50 messages (user + assistant). Each turn that
# pushes over the cap evicts the oldest message-pair from the front.
HISTORY_TURNS = 25
_MAX_MESSAGES = HISTORY_TURNS * 2

_lock = threading.Lock()


def _sessions_dir() -> Path:
    from agent_paths import config_dir

    p = config_dir() / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _file_for(session_key: str) -> Path:
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
    return _sessions_dir() / f"{digest}.json"


def load(session_key: str) -> List[Tuple[str, str]]:
    """Return the saved history as ``[(role, text), ...]``.

    ``role`` is ``"user"`` or ``"assistant"``. Returns an empty list when the
    session is new or the file is unreadable.
    """
    if not session_key:
        return []
    path = _file_for(session_key)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("session_store: could not read %s: %s", path, exc)
        return []
    msgs = data.get("messages") or []
    out: List[Tuple[str, str]] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip()
        text = str(m.get("text") or "")
        if role in ("user", "assistant") and text:
            out.append((role, text))
    return out


def append(session_key: str, user_text: str, assistant_text: str) -> None:
    """Add the new user/assistant pair to the session and trim to cap."""
    if not session_key:
        return
    path = _file_for(session_key)
    with _lock:
        history = load(session_key)
        now = time.time()
        history.append(("user", user_text))
        history.append(("assistant", assistant_text))
        # Trim from the front (oldest first) so we keep the most recent
        # ``_MAX_MESSAGES`` entries.
        if len(history) > _MAX_MESSAGES:
            history = history[-_MAX_MESSAGES:]
        payload = {
            "key": session_key,
            "updated_at": now,
            "messages": [
                {"role": role, "text": text} for role, text in history
            ],
        }
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("session_store: could not write %s: %s", path, exc)


def format_for_prompt(history: List[Tuple[str, str]], *, max_chars: int = 8000) -> str:
    """Render history as a plain-text block for the planner's system context.

    Older messages are dropped first if the formatted block would exceed
    ``max_chars`` -- token budget guardrail so a long-running session doesn't
    silently blow past the LLM's context window. The default 8 KB is well
    below DeepSeek/Claude limits while leaving room for the current message.
    """
    if not history:
        return ""
    lines: list[str] = []
    used = 0
    # Walk from newest to oldest so the most recent context is preserved
    # under the budget; then reverse for display order.
    for role, text in reversed(history):
        line = f"{'User' if role == 'user' else 'Assistant'}: {text}"
        if used + len(line) + 1 > max_chars and lines:
            break
        lines.append(line)
        used += len(line) + 1
    lines.reverse()
    return "Recent conversation:\n" + "\n".join(lines)


def clear(session_key: str) -> None:
    """Delete a session's history file. Idempotent."""
    if not session_key:
        return
    path = _file_for(session_key)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
