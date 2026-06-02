"""Environment-driven config for the web UI surface.

All knobs are read from the process environment so the same code runs in
dev (defaults) and prod (operator-set). Web users always run at
``WEB_PERMISSION_MODE`` — this is NOT user-selectable (see security model).
"""
from __future__ import annotations

import logging
import os
import secrets

log = logging.getLogger(__name__)

# Server-enforced permission tier for ALL web users. Not user-selectable.
WEB_PERMISSION_MODE = "workspace-write"

# Hard cap on a single user message (chars), checked before dispatch.
MAX_MESSAGE_CHARS = 8000

_DEV_SECRET: str | None = None


def _secret_file():
    from agent_paths import config_dir

    return config_dir() / "web" / "auth_secret"


def auth_secret() -> str:
    """JWT signing secret. Prefer ``WEB_AUTH_SECRET``; otherwise fall back to a
    secret PERSISTED on disk (``<config_dir>/web/auth_secret``).

    Persisting matters for two reasons the old ephemeral-per-process secret got
    wrong: tokens now survive a restart, and every uvicorn worker reads the same
    secret (a per-process random secret made worker A's tokens fail to verify on
    worker B). The on-disk fallback is still a dev convenience — production
    should set ``WEB_AUTH_SECRET`` explicitly (and ``web.__main__`` refuses a
    network bind without it)."""
    s = os.environ.get("WEB_AUTH_SECRET", "").strip()
    if s:
        return s
    global _DEV_SECRET
    if _DEV_SECRET is not None:
        return _DEV_SECRET
    path = _secret_file()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            _DEV_SECRET = existing
            return _DEV_SECRET
    except OSError:
        pass
    _DEV_SECRET = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEV_SECRET, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        log.warning(
            "WEB_AUTH_SECRET not set and could not persist a dev secret to %s; "
            "using an ephemeral one (tokens invalidated on restart).", path,
        )
        return _DEV_SECRET
    log.warning(
        "WEB_AUTH_SECRET not set; generated a persistent dev secret at %s. "
        "Set WEB_AUTH_SECRET explicitly in production.", path,
    )
    return _DEV_SECRET


def signup_code() -> str:
    """Optional registration gate. Blank = open registration."""
    return os.environ.get("WEB_SIGNUP_CODE", "").strip()


def rate_limit_per_min() -> int:
    """Per-user turn budget per minute. Defaults to 20; bad values fall back."""
    try:
        return int(os.environ.get("WEB_RATE_LIMIT_PER_MIN", "20"))
    except ValueError:
        return 20


def max_concurrency() -> int:
    """Max simultaneous web turns. Default 1 = today's serialized behavior
    (reversible rollout); raise ``WEB_MAX_CONCURRENCY`` to enable multi-user
    parallelism now that per-turn state lives on the TurnContext."""
    try:
        return max(1, int(os.environ.get("WEB_MAX_CONCURRENCY", "1")))
    except ValueError:
        return 1


def cookie_secure() -> bool:
    """Whether the session cookie carries the Secure flag. Default true; set
    ``WEB_COOKIE_SECURE=0`` for local http dev."""
    return os.environ.get("WEB_COOKIE_SECURE", "1").strip() != "0"
