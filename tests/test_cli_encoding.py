"""Coverage for the CLI's UTF-8-on-pipe shim.

The shim only fires when ``stream.isatty()`` is false (piped output). On
Windows hosts whose default code page is cp936, that flips stdout/stderr
to UTF-8 so a redirected ``python cli.py prompt > out.txt`` does not write
mojibake.

The e2e harness can't exercise this in practice — it sets
``PYTHONIOENCODING=utf-8`` on the subprocess, which pins the streams to
UTF-8 *before* ``_force_utf8_when_piped`` runs, masking any breakage in
the shim itself. These unit tests stand in for that gap.
"""
from __future__ import annotations

import io
import sys

import pytest

from cli import _force_utf8_when_piped


class _FakePipedStream:
    """TextIOWrapper-like double that reports as a pipe."""

    def __init__(self, encoding: str = "cp936"):
        self.encoding = encoding
        self.reconfigured_with: dict | None = None

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **kwargs) -> None:
        self.reconfigured_with = kwargs
        self.encoding = kwargs.get("encoding", self.encoding)


class _FakeTtyStream(_FakePipedStream):
    def isatty(self) -> bool:
        return True


def test_force_utf8_when_piped_flips_piped_streams(monkeypatch) -> None:
    fake_out, fake_err = _FakePipedStream(), _FakePipedStream()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)

    _force_utf8_when_piped()

    assert fake_out.reconfigured_with == {"encoding": "utf-8"}
    assert fake_err.reconfigured_with == {"encoding": "utf-8"}


def test_force_utf8_when_piped_skips_tty(monkeypatch) -> None:
    """Don't reconfigure an interactive console — that's where forcing UTF-8
    on a legacy cmd.exe actually causes on-screen mojibake."""
    fake_out, fake_err = _FakeTtyStream(), _FakeTtyStream()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)

    _force_utf8_when_piped()

    assert fake_out.reconfigured_with is None
    assert fake_err.reconfigured_with is None


def test_force_utf8_when_piped_swallows_stream_without_reconfigure(
    monkeypatch,
) -> None:
    """A captured stream (BytesIO/StringIO) lacks ``reconfigure``; the shim
    must catch AttributeError instead of crashing the CLI."""
    plain = io.StringIO()  # no isatty, no reconfigure
    monkeypatch.setattr(sys, "stdout", plain)
    monkeypatch.setattr(sys, "stderr", plain)

    # Should NOT raise.
    _force_utf8_when_piped()


def test_force_utf8_when_piped_swallows_oserror(monkeypatch) -> None:
    """Some embedded environments fail reconfigure with OSError; do not let
    that bubble up and crash the CLI before argparse runs."""
    class _OSErrStream(_FakePipedStream):
        def reconfigure(self, **kwargs):  # type: ignore[override]
            raise OSError("not supported")

    fake = _OSErrStream()
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.setattr(sys, "stderr", fake)

    # Should NOT raise.
    _force_utf8_when_piped()
