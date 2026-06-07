"""Inline arrow-key picker shared by /model and /gateway.

Gives the REPL a reusable arrow-key selection UX for slash commands.

Public API:
    can_use_interactive_picker() -> bool
    interactive_select(title, options, default_index=0, instruction=...) -> int | None
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable


_PICKER_VIEWPORT_ROWS = 18
_PICKER_FOOTER_ROWS = 8  # number of body rows reserved for the footer pane


def _run_blocking_app(app) -> None:
    """Run a ``prompt_toolkit.Application`` synchronously from either context.

    ``Application.run()`` calls ``asyncio.run()`` internally, which raises
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``
    when invoked from inside the multi-agent REPL (which is itself running
    under ``asyncio.run(run_repl())``).

    Mirror the trick :func:`orchestrator.repl_ui.ReplUI.read_input_async`
    already uses for the boxed input: if an outer loop is running, run the
    blocking app in a worker thread whose own ``asyncio.run`` is unblocked.
    The outer loop pauses for the duration, which is what we want anyway --
    the user is interacting with the menu.
    """
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if not in_loop:
        app.run()
        return

    err: list[BaseException] = []

    def _worker() -> None:
        try:
            app.run()
        except BaseException as exc:  # noqa: BLE001 - propagate after join
            err.append(exc)

    t = threading.Thread(target=_worker, name="picker-app", daemon=True)
    t.start()
    t.join()
    if err:
        raise err[0]


def can_use_interactive_picker() -> bool:
    """Whether the arrow-key picker can run in the current environment."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def interactive_select(
    title: str,
    options: list[tuple[str, str]],
    default_index: int = 0,
    instruction: str = "up/down move - enter select - esc cancel",
    *,
    footer_lines: Callable[[], list[str]] | None = None,
    footer_title: str | None = None,
    footer_refresh_seconds: float | None = None,
    footer_empty_message: str | None = None,
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
    - ``footer_refresh_seconds``: when set, the prompt_toolkit
      ``Application`` is constructed with this ``refresh_interval``, which
      re-invalidates the screen on a timer so ``footer_lines`` is re-run
      even when the user isn't pressing keys.
    - ``footer_empty_message``: text to show when ``footer_lines`` returns
      no rows (e.g. log file empty). ``None`` leaves the footer blank.
    """
    if not options:
        return None
    if not can_use_interactive_picker():
        raise RuntimeError("interactive_select requires a TTY")

    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import D
    from prompt_toolkit.styles import Style

    n = len(options)
    visible = min(n, _PICKER_VIEWPORT_ROWS)
    needs_scroll = n > visible

    cursor = [max(0, min(default_index, n - 1))]
    viewport = [max(0, min(cursor[0] - visible // 2, n - visible))]
    result: list[int | None] = [None]

    def render_title():
        return FormattedText([
            ("class:title", title + "\n"),
            ("class:hint", instruction + "\n"),
        ])

    def render_body():
        if needs_scroll:
            if cursor[0] < viewport[0]:
                viewport[0] = cursor[0]
            elif cursor[0] >= viewport[0] + visible:
                viewport[0] = cursor[0] - visible + 1
            viewport[0] = max(0, min(viewport[0], n - visible))
            start = viewport[0]
            end = start + visible
        else:
            start, end = 0, n

        lines: list[tuple[str, str]] = []
        if needs_scroll:
            if start > 0:
                lines.append(("class:hint", f"   ^ {start} more above\n"))
            else:
                lines.append(("", "\n"))

        for i in range(start, end):
            primary, secondary = options[i]
            if i == cursor[0]:
                marker = "> "
                row_style = "class:cursor"
                sec_style = "class:cursor"
            else:
                marker = "  "
                row_style = ""
                sec_style = "class:dim"
            lines.append((row_style, marker + primary))
            if secondary:
                lines.append((sec_style, "  " + secondary))
            lines.append(("", "\n"))

        if needs_scroll:
            remaining = n - end
            if remaining > 0:
                lines.append(("class:hint", f"   v {remaining} more below\n"))
            else:
                lines.append(("", "\n"))
        return FormattedText(lines)

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _(event):
        cursor[0] = (cursor[0] - 1) % n

    @kb.add("down")
    @kb.add("j")
    def _(event):
        cursor[0] = (cursor[0] + 1) % n

    @kb.add("pageup")
    def _(event):
        cursor[0] = max(0, cursor[0] - visible)

    @kb.add("pagedown")
    def _(event):
        cursor[0] = min(n - 1, cursor[0] + visible)

    @kb.add("home")
    @kb.add("g")
    def _(event):
        cursor[0] = 0

    @kb.add("end")
    @kb.add("G")
    def _(event):
        cursor[0] = n - 1

    @kb.add("space")
    @kb.add("enter")
    def _(event):
        result[0] = cursor[0]
        event.app.exit()

    @kb.add("c-c")
    @kb.add("escape")
    @kb.add("q")
    def _(event):
        result[0] = None
        event.app.exit()

    style = Style.from_dict({
        "cursor": "reverse bold",
        "title": "bold ansicyan",
        "hint": "ansibrightblack",
        "dim": "ansibrightblack",
    })

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
                if footer_empty_message:
                    return FormattedText([("class:dim", footer_empty_message + "\n")])
                return FormattedText([("", "")])
            return FormattedText([("class:dim", "\n".join(lines) + "\n")])

        if footer_title:
            windows.append(Window(content=FormattedTextControl(render_footer_title), height=1))
        windows.append(Window(
            content=FormattedTextControl(render_footer_body),
            height=D(preferred=_PICKER_FOOTER_ROWS, max=_PICKER_FOOTER_ROWS),
        ))

    layout = Layout(HSplit(windows))

    refresh_interval = (
        footer_refresh_seconds if footer_lines is not None else None
    )
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=False,
        refresh_interval=refresh_interval,
    )

    _run_blocking_app(app)
    return result[0]


async def interactive_select_async(
    title: str,
    options: list[tuple[str, str]],
    default_index: int = 0,
    instruction: str = "up/down move - enter select - esc cancel",
    *,
    footer_lines: Callable[[], list[str]] | None = None,
    footer_title: str | None = None,
    footer_refresh_seconds: float | None = None,
    footer_empty_message: str | None = None,
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
        footer_empty_message=footer_empty_message,
    )
