"""Tail + filter helper for the gateway action-menu log panel.

Reads ``<config_dir>/gateway.log`` from the end, keeps only lines that
match the requested platform, and returns at most ``max_lines`` already-
trimmed strings (chronological order). Designed for repeated calls from a
prompt_toolkit render loop, so it must be fast on a small file and never
raise.
"""
from __future__ import annotations

from pathlib import Path

from gateway._constants import GatewayPlatform

# Per-platform filter rules. A line is accepted if EITHER:
#   - the raw line contains any "marker" substring, OR
#   - the logger-name field (the 4th whitespace-separated token of a line
#     produced by gateway._constants.LOG_FORMAT) starts with one of the
#     listed prefixes OR equals one of the exact names.
#
# Keeping this as plain string ops (no regex) is intentional: the function
# runs ~5 times/sec from the picker's render loop.
_FILTERS: dict[str, dict[str, tuple[str, ...]]] = {
    "qq": {
        "markers": ("gateway[qq]",),
        "logger_prefixes": ("gateway.qq",),
        "logger_exact": ("qq",),
    },
    "feishu": {
        "markers": ("gateway[feishu]",),
        "logger_prefixes": ("gateway.feishu", "lark_oapi", "uvicorn"),
        "logger_exact": ("feishu",),
    },
}


def _logger_name(line: str) -> str:
    """Return the logger-name column from a formatter-shaped line, or ``""``.

    The format is "<date> <time>,<ms> <LEVEL> <name> | <message>". We just
    grab the 4th whitespace-separated token (index 3) — robust enough for
    log lines, harmless on malformed ones (returns "" → no prefix match).
    """
    parts = line.split(maxsplit=4)
    return parts[3] if len(parts) >= 4 else ""


def _matches(line: str, rule: dict[str, tuple[str, ...]]) -> bool:
    if any(marker in line for marker in rule["markers"]):
        return True
    name = _logger_name(line)
    if not name:
        return False
    if name in rule["logger_exact"]:
        return True
    return any(name.startswith(prefix) for prefix in rule["logger_prefixes"])


def _truncate(line: str, max_width: int | None) -> str:
    if max_width is None or max_width <= 0 or len(line) <= max_width:
        return line
    if max_width == 1:
        return "…"
    return line[: max_width - 1] + "…"


def read_tail(
    path: Path,
    *,
    platform: GatewayPlatform,
    max_lines: int = 8,
    max_width: int | None = None,
) -> list[str]:
    """Read ``path`` and return up to ``max_lines`` filtered lines.

    Returns chronological order (oldest → newest). Never raises: any IO
    or decode error yields an empty list, since the caller (picker
    footer) must not crash the UI on a bad log file.

    Unknown ``platform`` → empty list (defensive; today's only callers are
    "qq" and "feishu" but we don't want a silent KeyError if someone wires
    a new gateway without updating ``_FILTERS``).
    """
    rule = _FILTERS.get(platform)
    if rule is None:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not text:
        return []

    collected: list[str] = []
    # Walk newest → oldest so we can stop as soon as we have enough.
    for raw in reversed(text.splitlines()):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if not _matches(line, rule):
            continue
        collected.append(_truncate(line, max_width))
        if len(collected) >= max_lines:
            break
    collected.reverse()  # chronological for display
    return collected
