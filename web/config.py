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


class UnsafeExposureError(RuntimeError):
    """Raised when the server would bind to a network-reachable address without
    the secrets required to expose it safely."""


def is_loopback(host: str) -> bool:
    """True for binds reachable only from the local machine. ``0.0.0.0`` / ``::``
    (all-interfaces) and any concrete LAN/public address are NOT loopback."""
    h = (host or "").strip().strip("[]").lower()
    return h in ("127.0.0.1", "localhost", "::1")


def assert_safe_for_exposure(host: str) -> None:
    """Refuse a network-exposed bind without the mandatory secrets.

    On a non-loopback bind, anyone who can reach the port can register an
    account and drive a workspace-write agent (shell/file/python tools), so a
    persistent JWT secret AND a registration gate are required. Loopback binds
    stay zero-config for local dev. Single source of truth for "safe to expose"
    — both ``web.__main__`` and any embedding server call this."""
    if is_loopback(host):
        return
    missing = []
    if not os.environ.get("WEB_AUTH_SECRET", "").strip():
        missing.append("WEB_AUTH_SECRET")
    # Resolve the gate, not just the env var: a code set on disk by the toggle
    # counts as a real gate, so don't refuse exposure when one is configured.
    if not signup_code():
        missing.append("WEB_SIGNUP_CODE")
    if missing:
        raise UnsafeExposureError(
            f"Refusing to bind {host} (network-exposed) without "
            f"{' and '.join(missing)} set. Set them, or bind 127.0.0.1 for "
            "local-only use."
        )

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


def _signup_code_file():
    from agent_paths import config_dir

    return config_dir() / "web" / "signup_code"


def signup_code() -> str:
    """Optional registration gate. Blank = open registration.

    Prefer ``WEB_SIGNUP_CODE`` from the environment; otherwise fall back to a
    code PERSISTED on disk (``<config_dir>/web/signup_code``) — the same on-disk
    pattern as ``auth_secret``. The on-disk fallback is what the 邀请码开关
    toggle writes. An environment variable only reaches a process launched
    AFTER it was set, so a server that was already running when the operator
    flipped the toggle never saw the var and left registration open. Reading the
    file here resolves the gate per request from live on-disk state, so toggling
    it takes effect on the next registration with no server restart."""
    s = os.environ.get("WEB_SIGNUP_CODE", "").strip()
    if s:
        return s
    try:
        return _signup_code_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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


def pool_enabled() -> bool:
    """Whether to reuse bootstrapped specialist hosts across turns. Default off
    = today's per-turn cold spawn (reversible rollout); set WEB_POOL_ENABLED=1
    to remove the ~7s cold-start on most turns."""
    return os.environ.get("WEB_POOL_ENABLED", "0").strip() not in ("", "0")


def pool_max_hosts() -> int:
    """Global cap on live pooled hosts; LRU-evict the oldest idle over cap."""
    try:
        return max(1, int(os.environ.get("WEB_POOL_MAX_HOSTS", "8")))
    except ValueError:
        return 8


def pool_idle_ttl() -> float:
    """Seconds an idle pooled host survives before the sweeper shuts it down."""
    try:
        return max(1.0, float(os.environ.get("WEB_POOL_IDLE_TTL", "600")))
    except ValueError:
        return 600.0


def cookie_secure() -> bool:
    """Whether the session cookie carries the Secure flag. Default true; set
    ``WEB_COOKIE_SECURE=0`` for local http dev."""
    return os.environ.get("WEB_COOKIE_SECURE", "1").strip() != "0"
