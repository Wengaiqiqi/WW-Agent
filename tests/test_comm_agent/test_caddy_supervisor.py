"""Tests for caddy_supervisor.py (mock subprocess; we never spawn real caddy)."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.comm_agent.caddy_supervisor import (
    CaddyError, CaddySupervisor, render_caddyfile,
)


def test_render_caddyfile_with_public_host(tmp_path: Path) -> None:
    cfg = render_caddyfile(
        public_host="home.example.com",
        listen_port=8443,
        upstream_port=18080,
        access_log=tmp_path / "caddy-access.log",
    )
    assert "home.example.com:8443" in cfg
    assert "reverse_proxy localhost:18080" in cfg
    assert "caddy-access.log" in cfg


def test_render_caddyfile_no_host_auto_internal(tmp_path: Path) -> None:
    """public_host=None → bind :8443, internal cert (LAN/VPN scenario)."""
    cfg = render_caddyfile(
        public_host=None,
        listen_port=8443,
        upstream_port=18080,
        access_log=tmp_path / "caddy-access.log",
    )
    assert ":8443" in cfg
    assert "tls internal" in cfg


def test_supervisor_raises_when_caddy_missing(tmp_path: Path) -> None:
    sup = CaddySupervisor(
        caddyfile_path=tmp_path / "Caddyfile",
        binary="this-binary-does-not-exist-zzz",
    )
    with pytest.raises(CaddyError, match="not found"):
        sup.ensure_binary()


@pytest.mark.asyncio
async def test_supervisor_starts_and_stops(tmp_path: Path) -> None:
    """Mock subprocess.Popen to assert lifecycle without real caddy."""
    sup = CaddySupervisor(
        caddyfile_path=tmp_path / "Caddyfile",
        binary="/usr/bin/true",  # any always-available binary works for the mock
    )
    sup._caddyfile_content = "# rendered"
    # Patch shutil.which so the binary check passes regardless of host OS
    # (the plan's "/usr/bin/true" does not resolve on Windows).
    with patch("agents.comm_agent.caddy_supervisor.subprocess.Popen") as popen, \
            patch("agents.comm_agent.caddy_supervisor.shutil.which", return_value="/usr/bin/true"):
        proc = MagicMock()
        proc.pid = 12345
        proc.poll = MagicMock(return_value=None)
        proc.terminate = MagicMock()
        proc.wait = MagicMock(return_value=0)
        popen.return_value = proc
        await sup.start()
        assert popen.called
        # Caddyfile was written
        assert (tmp_path / "Caddyfile").exists()
        # caddy's stderr must NOT be an unread PIPE: caddy is chatty and the
        # ~64KB OS pipe buffer would fill and block it. It should go to a file
        # (or DEVNULL), never subprocess.PIPE.
        assert popen.call_args.kwargs.get("stderr") is not subprocess.PIPE
        await sup.stop()
        assert proc.terminate.called
