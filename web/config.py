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


def auth_secret() -> str:
    """JWT signing secret. Prefer ``WEB_AUTH_SECRET``; dev falls back to an
    ephemeral per-process secret (tokens won't survive a restart) with a warning."""
    s = os.environ.get("WEB_AUTH_SECRET", "").strip()
    if s:
        return s
    global _DEV_SECRET
    if _DEV_SECRET is None:
        _DEV_SECRET = secrets.token_urlsafe(32)
        log.warning(
            "WEB_AUTH_SECRET not set; using an ephemeral dev secret. "
            "Set WEB_AUTH_SECRET in production (tokens are invalidated on restart)."
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


def cookie_secure() -> bool:
    """Whether the session cookie carries the Secure flag. Default true; set
    ``WEB_COOKIE_SECURE=0`` for local http dev."""
    return os.environ.get("WEB_COOKIE_SECURE", "1").strip() != "0"
