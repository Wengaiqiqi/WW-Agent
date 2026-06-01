"""Render a Caddyfile and run Caddy as a child subprocess.

We do TLS termination via Caddy, not Python, because doing ACME +
certificate renewal correctly is a separate project. Caddy with a
two-line Caddyfile does it for us.

The supervisor:
  1. Renders the Caddyfile (string)
  2. Writes it next to the registry (so it persists across restarts)
  3. Spawns ``caddy run --config <path>`` as a child process
  4. On stop(), sends SIGTERM and waits
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class CaddyError(Exception):
    pass


_TEMPLATE_PUBLIC = """\
{public_host}:{listen_port} {{
    reverse_proxy localhost:{upstream_port}
    log {{
        output file {access_log}
        format json
    }}
}}
"""

_TEMPLATE_INTERNAL = """\
:{listen_port} {{
    tls internal
    reverse_proxy localhost:{upstream_port}
    log {{
        output file {access_log}
        format json
    }}
}}
"""


def render_caddyfile(
    *,
    public_host: str | None,
    listen_port: int,
    upstream_port: int,
    access_log: Path,
) -> str:
    """Return a Caddyfile string for the comm-agent upstream."""
    if public_host:
        return _TEMPLATE_PUBLIC.format(
            public_host=public_host,
            listen_port=listen_port,
            upstream_port=upstream_port,
            access_log=str(access_log).replace("\\", "/"),
        )
    return _TEMPLATE_INTERNAL.format(
        listen_port=listen_port,
        upstream_port=upstream_port,
        access_log=str(access_log).replace("\\", "/"),
    )


class CaddySupervisor:
    def __init__(self, *, caddyfile_path: Path, binary: str = "caddy"):
        self._caddyfile_path = caddyfile_path
        self._binary = binary
        self._caddyfile_content: str | None = None
        self._proc: subprocess.Popen | None = None
        # File handle for caddy's stderr. See start() for why we don't PIPE it.
        self._stderr_log = None

    def set_caddyfile(self, content: str) -> None:
        self._caddyfile_content = content

    def ensure_binary(self) -> None:
        """Raise CaddyError if the caddy binary isn't on PATH."""
        # shutil.which handles both an absolute path and a PATH lookup.
        if not shutil.which(self._binary):
            raise CaddyError(
                f"caddy binary {self._binary!r} not found on PATH; install Caddy "
                f"(see https://caddyserver.com/docs/install) or set CADDY_BINARY env var"
            )

    async def start(self) -> None:
        if self._caddyfile_content is None:
            raise CaddyError("set_caddyfile() must be called before start()")
        self.ensure_binary()
        self._caddyfile_path.parent.mkdir(parents=True, exist_ok=True)
        self._caddyfile_path.write_text(self._caddyfile_content, encoding="utf-8")
        # caddy logs to stderr. A ``subprocess.PIPE`` we never read fills its
        # ~64KB OS buffer within seconds of a chatty startup (ACME, cert loads)
        # and then BLOCKS caddy forever. Redirect to a file instead so the
        # buffer can't back up AND startup/cert errors stay inspectable.
        stderr_log_path = self._caddyfile_path.parent / "caddy.stderr.log"
        self._stderr_log = stderr_log_path.open("ab")
        # subprocess.Popen is blocking on Windows even for the fork — run in
        # the default executor so the asyncio loop stays responsive while
        # caddy initialises ACME / loads cert files.
        loop = asyncio.get_running_loop()
        self._proc = await loop.run_in_executor(
            None,
            lambda: subprocess.Popen(
                [self._binary, "run", "--config", str(self._caddyfile_path)],
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_log,
            ),
        )
        log.info("caddy started, pid=%s (stderr -> %s)", self._proc.pid, stderr_log_path)

    async def stop(self) -> None:
        if self._proc is None:
            self._close_stderr_log()
            return
        loop = asyncio.get_running_loop()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                await loop.run_in_executor(
                    None, lambda: self._proc.wait(timeout=5),
                )
            except subprocess.TimeoutExpired:
                log.warning("caddy did not exit in 5s — killing")
                self._proc.kill()
                # Wait for the OS to actually reap the killed process; without
                # this, the :443/:8443 socket can still be in TIME_WAIT/in-use
                # when the next start() runs, producing EADDRINUSE.
                try:
                    await loop.run_in_executor(
                        None, lambda: self._proc.wait(timeout=5),
                    )
                except subprocess.TimeoutExpired:
                    log.error("caddy did not exit after kill — leaking pid=%s",
                              self._proc.pid)
        self._proc = None
        self._close_stderr_log()

    def _close_stderr_log(self) -> None:
        if self._stderr_log is not None:
            try:
                self._stderr_log.close()
            except OSError:
                pass
            self._stderr_log = None
