"""Cross-process PID lock for gateway adapters.

Prevents the embarrassing "I started two of these and the bot replied N times"
class of bug. Each platform writes its PID to ``.langchain-agent/<platform>.pid``
on start; subsequent starts in another process see that file, check whether
the listed PID is still alive, and refuse if so.

Stale PID files (process is gone) are reaped silently — common after a Ctrl+C
or a crash.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _pid_file(platform: str) -> Path:
    from agent_paths import config_dir

    return config_dir() / f"{platform}.pid"


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # ``os.kill(pid, 0)`` on Windows is broken pre-3.11; use a probe via
            # OpenProcess. On any Python where ctypes is available this works.
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class AlreadyRunning(RuntimeError):
    """Raised when a gateway is asked to start but another instance owns the lock."""


def acquire(platform: str) -> Path:
    """Take the PID lock for ``platform``.

    Raises :class:`AlreadyRunning` if another live process holds it. Returns
    the path on success; caller is responsible for releasing on shutdown.
    """
    path = _pid_file(platform)
    if path.exists():
        try:
            existing = int(path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            existing = 0
        if existing and existing != os.getpid() and _is_pid_alive(existing):
            raise AlreadyRunning(
                f"another gateway[{platform}] is already running (pid={existing}). "
                f"Stop it first, or run: Stop-Process -Id {existing} -Force"
            )
        # Stale or self-owned -- safe to overwrite.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def release(platform: str) -> None:
    """Remove the PID file. Idempotent; ignores I/O errors."""
    try:
        p = _pid_file(platform)
        if p.exists():
            try:
                owner = int(p.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                owner = 0
            # Only remove if we still own it (defensive against another
            # process having taken it after we noticed it was stale).
            if owner in (0, os.getpid()):
                p.unlink()
    except OSError:
        pass
