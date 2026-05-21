# Gateway Action Menu Live Log Tail — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live-tailing log panel (last 8 platform-filtered lines from `gateway.log`) below the `/gateway → QQ/Feishu` action menu.

**Architecture:** A new `gateway.log_tail.read_tail()` pure function reads `<config_dir>/gateway.log` from the end and filters by platform. `orchestrator/picker.py` gets new opt-in `footer_*` kwargs to render a refreshable footer pane below the picker body. `orchestrator/repl_commands._gw_platform_menu` wires the two together. All other picker call sites stay untouched.

**Tech Stack:** Python 3.10+, `prompt_toolkit` (already used by `picker.py`), `pytest` for unit tests on `read_tail`.

**Spec:** `docs/superpowers/specs/2026-05-21-gateway-menu-log-tail-design.md`

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `gateway/log_tail.py` | Pure helper: `read_tail(path, platform, max_lines, max_width)` → `list[str]`. No imports from `orchestrator/` or `prompt_toolkit`. |
| Create | `tests/test_gateway_log_tail.py` | Unit tests covering filter rules, max_lines, max_width truncation, missing file, decode errors. |
| Modify | `orchestrator/picker.py` | Add three opt-in kwargs (`footer_lines`, `footer_title`, `footer_refresh_seconds`) to `interactive_select` and `interactive_select_async`. When set, render an extra Window pair (title + body) in the HSplit and schedule periodic `app.invalidate()` for live refresh. Default `None` → identical behavior to today. |
| Modify | `orchestrator/repl_commands.py` (`_gw_platform_menu` only — currently `repl_commands.py:535-595`) | Pass a `_footer` closure that calls `read_tail(...)` for the current platform, plus `footer_title="Recent log (last 8 lines, filtered)"` and `footer_refresh_seconds=0.2`. Long lines are truncated to `console.width - 4` inside `read_tail` via its `max_width` parameter. |

Existing untouched callers of the picker (`_gw_pick_platform`, `_gw_pick_feishu_mode`, legacy `/model` picker in `legacy/single_agent_loop.py` if any) must continue to work without changes.

---

## Task 1: `read_tail` — failing tests first

**Files:**
- Create: `gateway/log_tail.py` (empty stub)
- Create: `tests/test_gateway_log_tail.py`

- [ ] **Step 1: Create the empty module so imports resolve**

Create `gateway/log_tail.py` with:

```python
"""Tail + filter helper for the gateway action-menu log panel.

Reads ``<config_dir>/gateway.log`` from the end, keeps only lines that
match the requested platform, and returns at most ``max_lines`` already-
trimmed strings (chronological order). Designed for repeated calls from a
prompt_toolkit render loop, so it must be fast on a small file and never
raise.
"""
from __future__ import annotations
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_gateway_log_tail.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

```
pytest tests/test_gateway_log_tail.py -v
```

Expected: ImportError / "cannot import name 'read_tail' from 'gateway.log_tail'" on every test.

- [ ] **Step 4: Commit the failing tests**

```bash
git add gateway/log_tail.py tests/test_gateway_log_tail.py
git commit -m "test(gateway): add failing tests for log_tail.read_tail"
```

---

## Task 2: `read_tail` — implementation

**Files:**
- Modify: `gateway/log_tail.py`

- [ ] **Step 1: Replace the stub with the full implementation**

Overwrite `gateway/log_tail.py` with:

```python
"""Tail + filter helper for the gateway action-menu log panel.

Reads ``<config_dir>/gateway.log`` from the end, keeps only lines that
match the requested platform, and returns at most ``max_lines`` already-
trimmed strings (chronological order). Designed for repeated calls from a
prompt_toolkit render loop, so it must be fast on a small file and never
raise.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

# Per-platform filter rules. A line is accepted if EITHER:
#   - the raw line contains any "marker" substring, OR
#   - the logger-name field (3rd whitespace-separated column of the formatter
#     "%(asctime)s %(levelname)-7s %(name)s | %(message)s") starts with one
#     of the listed prefixes OR equals one of the exact names.
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
    platform: str,
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
    except (FileNotFoundError, OSError, PermissionError):
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
```

- [ ] **Step 2: Run tests to verify they pass**

```
pytest tests/test_gateway_log_tail.py -v
```

Expected: all 14 tests pass.

- [ ] **Step 3: Commit**

```bash
git add gateway/log_tail.py
git commit -m "feat(gateway): add log_tail.read_tail with platform filtering"
```

---

## Task 3: Picker footer support — wiring

**Files:**
- Modify: `orchestrator/picker.py`

Today the file uses `from __future__ import annotations` (line 14) and imports `asyncio` (line 16). We need `from collections.abc import Callable` for the new type hint and to keep the existing `_PICKER_VIEWPORT_ROWS = 18` (line 21) intact.

- [ ] **Step 1: Add the Callable import and a footer-row constant**

In `orchestrator/picker.py`, find:

```python
from __future__ import annotations

import asyncio
import sys
import threading


_PICKER_VIEWPORT_ROWS = 18
```

Replace with:

```python
from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable


_PICKER_VIEWPORT_ROWS = 18
_PICKER_FOOTER_ROWS = 8  # number of body rows reserved for the footer pane
```

- [ ] **Step 2: Extend `interactive_select` signature**

Replace the existing signature and docstring (currently `orchestrator/picker.py:68-80`):

```python
def interactive_select(
    title: str,
    options: list[tuple[str, str]],
    default_index: int = 0,
    instruction: str = "up/down move - enter select - esc cancel",
) -> int | None:
    """Inline arrow-key picker built on prompt_toolkit.

    ``options`` is a list of ``(primary, secondary)`` rows; ``secondary`` may
    be empty. Returns the chosen index, or ``None`` when the user pressed
    Esc / q / Ctrl+C. Callers must check :func:`can_use_interactive_picker`
    first; invoking this without a TTY raises ``RuntimeError``.
    """
```

with:

```python
def interactive_select(
    title: str,
    options: list[tuple[str, str]],
    default_index: int = 0,
    instruction: str = "up/down move - enter select - esc cancel",
    *,
    footer_lines: Callable[[], list[str]] | None = None,
    footer_title: str | None = None,
    footer_refresh_seconds: float | None = None,
) -> int | None:
    """Inline arrow-key picker built on prompt_toolkit.

    ``options`` is a list of ``(primary, secondary)`` rows; ``secondary`` may
    be empty. Returns the chosen index, or ``None`` when the user pressed
    Esc / q / Ctrl+C. Callers must check :func:`can_use_interactive_picker`
    first; invoking this without a TTY raises ``RuntimeError``.

    Optional footer pane (used by ``/gateway`` to tail ``gateway.log``):

    - ``footer_lines``: callable invoked on every render; returns the lines
      to display below the picker body. ``None`` (default) hides the
      footer entirely — layout unchanged for legacy callers.
    - ``footer_title``: single-line header rendered above the footer.
    - ``footer_refresh_seconds``: when set, the picker schedules a periodic
      ``app.invalidate()`` so the footer re-runs ``footer_lines`` even when
      the user isn't pressing keys.
    """
```

- [ ] **Step 3: Build the footer layout block**

Find the layout-construction block (currently `orchestrator/picker.py:201-208`):

```python
    body_height = visible + (2 if needs_scroll else 0)
    layout = Layout(HSplit([
        Window(content=FormattedTextControl(render_title), height=2),
        Window(
            content=FormattedTextControl(render_body),
            height=D(preferred=body_height, max=body_height),
        ),
    ]))
```

Replace with:

```python
    body_height = visible + (2 if needs_scroll else 0)

    windows = [
        Window(content=FormattedTextControl(render_title), height=2),
        Window(
            content=FormattedTextControl(render_body),
            height=D(preferred=body_height, max=body_height),
        ),
    ]

    if footer_lines is not None:
        def render_footer_title():
            return FormattedText([("class:hint", (footer_title or "") + "\n")])

        def render_footer_body():
            try:
                lines = footer_lines() or []
            except Exception:  # noqa: BLE001 - footer must not crash the UI
                lines = []
            if not lines:
                return FormattedText([
                    ("class:dim", "(no log yet — start the gateway to see activity)\n"),
                ])
            return FormattedText([("class:dim", "\n".join(lines) + "\n")])

        if footer_title:
            windows.append(Window(content=FormattedTextControl(render_footer_title), height=1))
        windows.append(Window(
            content=FormattedTextControl(render_footer_body),
            height=D(preferred=_PICKER_FOOTER_ROWS, max=_PICKER_FOOTER_ROWS),
        ))

    layout = Layout(HSplit(windows))
```

- [ ] **Step 4: Wire periodic refresh**

Find the final block that builds and runs the Application (currently `orchestrator/picker.py:210-215`):

```python
    _run_blocking_app(
        Application(
            layout=layout, key_bindings=kb, style=style, full_screen=False,
        )
    )
    return result[0]
```

Replace with:

```python
    app = Application(
        layout=layout, key_bindings=kb, style=style, full_screen=False,
    )

    if footer_lines is not None and footer_refresh_seconds is not None:
        async def _ticker() -> None:
            try:
                while True:
                    await asyncio.sleep(footer_refresh_seconds)
                    app.invalidate()
            except asyncio.CancelledError:
                raise

        # Install the refresh task on the first paint — by then the
        # application has its asyncio loop bound and ``create_background_task``
        # is safe to call. The flag prevents re-installation on every render.
        installed: list[bool] = [False]

        def _install_once(_app) -> None:
            if installed[0]:
                return
            installed[0] = True
            app.create_background_task(_ticker())

        app.before_render += _install_once

    _run_blocking_app(app)
    return result[0]
```

- [ ] **Step 5: Extend `interactive_select_async` signature**

Find the async wrapper (currently `orchestrator/picker.py:218-253`) and replace it with:

```python
async def interactive_select_async(
    title: str,
    options: list[tuple[str, str]],
    default_index: int = 0,
    instruction: str = "up/down move - enter select - esc cancel",
    *,
    footer_lines: Callable[[], list[str]] | None = None,
    footer_title: str | None = None,
    footer_refresh_seconds: float | None = None,
) -> int | None:
    """Async variant for slash-command handlers that run inside the REPL loop.

    Implementation: delegates the *entire* picker to the synchronous
    :func:`interactive_select` running in a worker thread via
    ``asyncio.to_thread``. The worker has no running asyncio loop, so the
    sync picker takes its "no outer loop" path -- a brand-new asyncio
    loop is created INSIDE the worker thread just for the prompt_toolkit
    Application, completely isolated from the REPL's main loop.

    This sidesteps a real bug with the prior ``Application.run_async()``
    approach: prompt_toolkit installs stdin / signal hooks on the loop
    it runs on, and on exit doesn't always clean up perfectly. Doing it
    on the REPL's main loop left residue that broke later async-generator
    based work (notably the SSE stream :func:`_delegate_to_agent` uses to
    talk to tool-agent). With the worker-thread approach, the loop with
    those hooks is torn down completely when the picker exits.

    The REPL's main loop continues processing OTHER tasks (gateway
    WebSocket reads, reply POSTs) during ``await asyncio.to_thread(...)``
    because to_thread just suspends the awaiting task. So the original
    motivation -- "gateway must keep ticking while menu is up" -- still
    holds.

    Footer kwargs forward to :func:`interactive_select` unchanged.
    """
    return await asyncio.to_thread(
        interactive_select,
        title,
        options,
        default_index=default_index,
        instruction=instruction,
        footer_lines=footer_lines,
        footer_title=footer_title,
        footer_refresh_seconds=footer_refresh_seconds,
    )
```

- [ ] **Step 6: Smoke test the picker still works for non-footer callers**

Run the existing repl-types / picker-related test files to confirm no regression:

```
pytest tests/test_orchestrator/test_repl_types.py tests/test_orchestrator/test_ui_input.py -v
```

Expected: existing tests pass (the picker is not unit-tested directly, but
nothing that imports `orchestrator.picker` should break).

Also confirm the module still imports cleanly:

```
python -c "from orchestrator.picker import interactive_select, interactive_select_async; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/picker.py
git commit -m "feat(picker): opt-in live-refresh footer pane via footer_* kwargs"
```

---

## Task 4: Wire the footer into `/gateway` action menu

**Files:**
- Modify: `orchestrator/repl_commands.py` (only `_gw_platform_menu`, currently `orchestrator/repl_commands.py:535-595`)

- [ ] **Step 1: Add the two new imports near the top of `_gw_platform_menu`**

The existing imports inside `_gw_platform_menu` are local (function-scoped) — see `orchestrator/repl_commands.py:540-541`:

```python
            from gateway import credentials as gw_creds
            from gateway.manager import get_manager
```

Right before the `while True:` loop (currently at `orchestrator/repl_commands.py:539`), insert the two extra imports so they're hoisted once per menu open instead of re-imported every iteration. Replace:

```python
    async def _gw_platform_menu(self, platform: str) -> bool:
        """Action menu for one platform. Returns True to redraw the platform list."""
        from orchestrator.picker import interactive_select_async

        while True:
            from gateway import credentials as gw_creds
            from gateway.manager import get_manager
```

with:

```python
    async def _gw_platform_menu(self, platform: str) -> bool:
        """Action menu for one platform. Returns True to redraw the platform list."""
        from orchestrator.picker import interactive_select_async
        from agent_paths import config_dir
        from gateway.log_tail import read_tail

        log_path = config_dir() / "gateway.log"

        def _footer() -> list[str]:
            # Truncate at console width - 4 so the panel never wraps and
            # break the picker layout. Width is sampled per call so the
            # user can resize the terminal during the session.
            return read_tail(
                log_path,
                platform=platform,
                max_lines=8,
                max_width=max(20, self.ui.console.width - 4),
            )

        while True:
            from gateway import credentials as gw_creds
            from gateway.manager import get_manager
```

- [ ] **Step 2: Pass the footer kwargs to `interactive_select_async`**

Find the existing call (currently `orchestrator/repl_commands.py:572-578`):

```python
            rows = [(label, hint) for _, label, hint in actions]
            self.ui.console.print()
            idx = await interactive_select_async(
                f"{label} -- choose action",
                rows,
                default_index=0,
                instruction="up/down move - enter run - esc back",
            )
```

Replace with:

```python
            rows = [(label, hint) for _, label, hint in actions]
            self.ui.console.print()
            idx = await interactive_select_async(
                f"{label} -- choose action",
                rows,
                default_index=0,
                instruction="up/down move - enter run - esc back",
                footer_lines=_footer,
                footer_title="Recent log (last 8 lines, filtered)",
                footer_refresh_seconds=0.2,
            )
```

- [ ] **Step 3: Import smoke test**

```
python -c "from orchestrator.repl_commands import ReplCommands; print('ok')"
```

Expected: prints `ok`. If it fails on import (e.g., circular import via
`gateway.log_tail`), move the `from gateway.log_tail import read_tail`
line inside `_footer` (lazy import). Re-run.

- [ ] **Step 4: Manual UI smoke test (cannot be unit-tested)**

In a real terminal:

1. Run the REPL: `python cli.py` (or whatever the project's entry point is — check `cli.py:1` if unsure).
2. Type `/gateway`, press Enter.
3. Select **QQ Official Bot**.
4. Confirm:
   - You see "Recent log (last 8 lines, filtered)" below the 5 action rows.
   - If `.langchain-agent/gateway.log` does not exist yet, the panel shows `(no log yet — start the gateway to see activity)`.
5. Pick **Setup credentials** (or skip if QQ is already configured) and **Start gateway**. Return to the action menu.
6. Watch the footer for ~5 seconds — it should populate with `gateway[qq]` lines as the WS connects. New lines appear without any key press (live refresh).
7. Press Esc, return to the platform list, choose **Feishu / Lark**, repeat the start flow. The Feishu menu's footer must NOT show QQ lines — only `gateway.feishu` / `lark_oapi.*` / `uvicorn.*`.
8. Resize the terminal narrower — lines truncate with `…`, panel does not wrap.
9. Stop both gateways via the menu — footer freezes at the last 8 entries, no crash.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py
git commit -m "feat(gateway): live-tail gateway.log under QQ/Feishu action menu"
```

---

## Task 5: Index update

**Files:**
- Modify: `gateway/README.md` (if it documents the menu — check first; skip if not relevant)

- [ ] **Step 1: Check whether the README mentions the action menu**

```
grep -n "choose action\|Setup credentials\|gateway.log" gateway/README.md
```

If there's an existing section describing the action menu, append one sentence at the end of that section:

```markdown
The action menu shows a live tail of the most recent 8 lines from
`<config_dir>/gateway.log`, filtered to the platform you opened — useful
for confirming WS connect / event delivery without leaving the REPL.
```

If the README doesn't describe the action menu at all, skip this step and the commit.

- [ ] **Step 2: Commit if the README was updated**

```bash
git add gateway/README.md
git commit -m "docs(gateway): mention live log tail in action menu"
```

---

## Self-Review

**Spec coverage:**

- `gateway/log_tail.py` with `read_tail(path, platform, max_lines, max_width)` → Task 2.
- Filter rules for `qq` / `feishu` → Task 2 (`_FILTERS` dict).
- Returns `[]` on missing / unreadable file → Task 2 (`try/except`) + Task 1 test `test_file_missing_returns_empty`.
- Picker gets opt-in `footer_lines` / `footer_title` / `footer_refresh_seconds` → Task 3.
- Backwards-compat for other picker callers → Task 3 (all three kwargs default `None`).
- `_gw_platform_menu` wires footer + 0.2s refresh + 8 lines + width truncation → Task 4.
- Placeholder `(no log yet — start the gateway to see activity)` → Task 3 `render_footer_body`.
- Unicode decode safety (`errors="replace"`) → Task 2 + test `test_unicode_decode_replace_does_not_raise`.
- Truncation with `…` → Task 2 `_truncate` + test `test_max_width_truncates_with_ellipsis`.
- Unit tests cover all rules listed in spec table → Task 1 (14 tests).
- Manual UI test plan → Task 4 Step 4.

**Placeholder scan:** No "TBD", "implement later", "etc." in any code block. Every step that changes code shows the full block. Tests have concrete asserts, not "assert it works".

**Type consistency:** `read_tail` signature is identical in spec, in the implementation (Task 2), and in the caller (`_footer` in Task 4). Footer kwargs are named identically in `interactive_select`, `interactive_select_async`, and the gateway caller. `_FILTERS` keys (`"qq"`, `"feishu"`) match the gateway platform slugs used elsewhere (`orchestrator/repl_commands.py:472-475`).

---

## Plan complete

Saved to `docs/superpowers/plans/2026-05-21-gateway-menu-log-tail.md`. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch with checkpoints.

Which approach?
