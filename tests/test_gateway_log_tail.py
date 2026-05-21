"""Tests for gateway/log_tail.py — tail + platform filtering for the picker footer."""
from __future__ import annotations

from pathlib import Path

import pytest

from gateway.log_tail import read_tail


# Mirrors the formatter installed by gateway.manager._install_file_logging:
#   "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
def _fmt(name: str, message: str, *, level: str = "INFO", ts: str = "2026-05-21 10:00:00,000") -> str:
    return f"{ts} {level:<7s} {name} | {message}"


def _write(tmp_path: Path, *lines: str) -> Path:
    p = tmp_path / "gateway.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_file_missing_returns_empty(tmp_path: Path) -> None:
    assert read_tail(tmp_path / "nope.log", platform="qq", max_lines=8) == []


def test_qq_filter_matches_bracket_marker(tmp_path: Path) -> None:
    # The QQ adapter logs "gateway[qq] ..." messages via the root gateway logger.
    p = _write(tmp_path, _fmt("gateway", "gateway[qq] connecting"))
    out = read_tail(p, platform="qq", max_lines=8)
    assert len(out) == 1
    assert "gateway[qq] connecting" in out[0]


def test_qq_filter_matches_logger_name(tmp_path: Path) -> None:
    p = _write(tmp_path, _fmt("gateway.qq", "WS connected"))
    out = read_tail(p, platform="qq", max_lines=8)
    assert len(out) == 1


def test_qq_filter_rejects_feishu(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        _fmt("gateway.feishu", "lark event"),
        _fmt("lark_oapi.ws", "heartbeat"),
    )
    assert read_tail(p, platform="qq", max_lines=8) == []


def test_feishu_filter_matches_lark_oapi(tmp_path: Path) -> None:
    p = _write(tmp_path, _fmt("lark_oapi.ws.client", "connected"))
    out = read_tail(p, platform="feishu", max_lines=8)
    assert len(out) == 1


def test_feishu_filter_matches_uvicorn(tmp_path: Path) -> None:
    p = _write(tmp_path, _fmt("uvicorn.access", '127.0.0.1 "POST /feishu/webhook"'))
    out = read_tail(p, platform="feishu", max_lines=8)
    assert len(out) == 1


def test_feishu_filter_rejects_qq(tmp_path: Path) -> None:
    p = _write(tmp_path, _fmt("gateway", "gateway[qq] hi"))
    assert read_tail(p, platform="feishu", max_lines=8) == []


def test_max_lines_caps_and_keeps_chronological_order(tmp_path: Path) -> None:
    lines = [_fmt("gateway.qq", f"event {i}") for i in range(20)]
    p = _write(tmp_path, *lines)
    out = read_tail(p, platform="qq", max_lines=8)
    assert len(out) == 8
    assert "event 12" in out[0]
    assert "event 19" in out[-1]


def test_max_width_truncates_with_ellipsis(tmp_path: Path) -> None:
    p = _write(tmp_path, _fmt("gateway.qq", "x" * 200))
    out = read_tail(p, platform="qq", max_lines=8, max_width=40)
    assert len(out[0]) == 40
    assert out[0].endswith("…")


def test_max_width_none_does_not_truncate(tmp_path: Path) -> None:
    p = _write(tmp_path, _fmt("gateway.qq", "x" * 200))
    out = read_tail(p, platform="qq", max_lines=8)
    assert "x" * 200 in out[0]


def test_unicode_decode_replace_does_not_raise(tmp_path: Path) -> None:
    # Write a valid line, then append a stray 0xFF byte (invalid UTF-8).
    p = tmp_path / "gateway.log"
    p.write_bytes(_fmt("gateway.qq", "ok").encode("utf-8") + b"\n" + b"\xff\n")
    out = read_tail(p, platform="qq", max_lines=8)
    # First line still parses; second line is decoded with replacement and gets filtered out
    # because it lacks the qq marker after replacement.
    assert any("ok" in line for line in out)


def test_empty_file_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "gateway.log"
    p.write_text("", encoding="utf-8")
    assert read_tail(p, platform="qq", max_lines=8) == []


def test_blank_lines_ignored(tmp_path: Path) -> None:
    p = _write(tmp_path, "", _fmt("gateway.qq", "hi"), "")
    out = read_tail(p, platform="qq", max_lines=8)
    assert len(out) == 1


def test_unknown_platform_returns_empty(tmp_path: Path) -> None:
    p = _write(tmp_path, _fmt("gateway.qq", "hi"))
    assert read_tail(p, platform="discord", max_lines=8) == []
