"""Inline boxed input prompt (Claude Code style).

Extracted from ``legacy/single_agent_loop.py`` so the multi-agent REPL no
longer has to reach back into the deprecated single-agent module to read a
line. The new home also makes the helper accept the slash-command dict as a
parameter, so the orchestrator can pass its OWN command catalog rather than
inheriting the legacy single-agent one (a small bug fix that fell out of the
extraction).
"""
from __future__ import annotations

from typing import Mapping


def _make_slash_completer(commands: Mapping[str, str]):
    """Build a prompt_toolkit Completer yielding slash commands with description meta.

    Subclassing prompt_toolkit's ``Completer`` is required so that
    ``get_completions_async`` (used when ``complete_while_typing=True``) is
    available via the base class.
    """
    from prompt_toolkit.completion import Completer, Completion

    class SlashCommandCompleter(Completer):
        def __init__(self) -> None:
            self.commands = commands

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            stripped = text.lstrip()
            # Only complete slash commands at the very start of the input line.
            if not stripped.startswith("/") or " " in stripped:
                return
            for cmd, desc in self.commands.items():
                if cmd.startswith(stripped.lower()):
                    yield Completion(
                        cmd,
                        start_position=-len(stripped),
                        display=cmd,
                        display_meta=desc,
                    )

    return SlashCommandCompleter()


def ask_boxed_input(
    history,
    *,
    label: str = "",
    commands: Mapping[str, str] | None = None,
    console=None,
) -> str:
    """Read a single user submission inside a bordered input box.

    Each call builds a fresh non-fullscreen ``prompt_toolkit.Application`` whose
    layout is a ``Frame`` wrapping a multi-line input window. The application
    renders inline at the current cursor position, so:

    - When prior output is short, the box sits right after it (no padding).
    - When prior output already fills the screen, normal terminal scrolling
      pushes the box to the visible bottom on its own.
    - Slash-command completions float above the cursor via a ``CompletionsMenu``.

    Returns the submitted text. Raises ``KeyboardInterrupt`` on Ctrl+C and
    ``EOFError`` on Ctrl+D against an empty buffer.

    ``commands`` is the slash-command catalog used for completion; passing it
    in (rather than reading a module-level constant) lets the multi-agent and
    single-agent REPLs each present their own command set without the helper
    knowing about either of them.

    ``console`` is the Rich Console used by Ctrl+L to clear the terminal; when
    omitted, falls back to a fresh ``rich.console.Console()``.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import (
        Float,
        FloatContainer,
        HSplit,
        Window,
    )
    from prompt_toolkit.layout.controls import BufferControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.layout.processors import (
        BeforeInput,
        ConditionalProcessor,
    )
    from prompt_toolkit.filters import has_focus
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import Frame

    if console is None:
        from rich.console import Console
        console = Console()

    completer = _make_slash_completer(commands or {})
    buf = Buffer(
        multiline=True,
        history=history,
        completer=completer,
        complete_while_typing=True,
    )

    kb = KeyBindings()

    @kb.add("c-c")
    def _(event) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    @kb.add("c-d")
    def _(event) -> None:
        # Only EOF when the buffer is empty (matches readline convention).
        if not buf.text:
            event.app.exit(exception=EOFError)

    @kb.add("c-l")
    def _(event) -> None:
        # Clear the terminal then redraw the box.
        console.clear()
        event.app.invalidate()

    @kb.add("enter")
    def _(event) -> None:
        text = buf.text
        # Trailing single backslash → newline; matches editor convention.
        if text.endswith("\\") and not text.endswith("\\\\"):
            buf.delete_before_cursor(1)
            buf.insert_text("\n")
            return
        event.app.exit(result=text)

    @kb.add("c-j")
    def _(event) -> None:
        buf.insert_text("\n")

    @kb.add("escape", "enter")
    def _(event) -> None:
        buf.insert_text("\n")

    before_input = BeforeInput(text="▌ ", style="class:prompt-mark")
    input_control = BufferControl(
        buffer=buf,
        input_processors=[ConditionalProcessor(before_input, has_focus(buf))],
    )

    # Shrink-to-content height: number of newlines in the buffer + 1, clamped
    # between 1 and 8. Recomputed on every render so the frame grows as the
    # user types newlines and stays tight when empty.
    def _calc_input_height() -> Dimension:
        line_count = buf.text.count("\n") + 1
        rows = min(max(line_count, 1), 8)
        return Dimension.exact(rows)

    input_window = Window(
        content=input_control,
        wrap_lines=True,
        height=_calc_input_height,
    )

    framed = Frame(
        input_window,
        style="class:input-frame",
        title=f" {label} " if label else None,
    )
    body = HSplit([framed])

    layout = Layout(
        FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8, scroll_offset=1),
                ),
            ],
        )
    )

    style = Style.from_dict(
        {
            "input-frame frame.border": "#5fafff",
            "prompt-mark": "bold #5fafff",
            "completion-menu.completion": "bg:#222222 #cccccc",
            "completion-menu.completion.current": "bg:#5fafff #000000",
            "completion-menu.meta.completion": "bg:#222222 #888888",
            "completion-menu.meta.completion.current": "bg:#5fafff #000000",
        }
    )

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=False,
        mouse_support=False,
        erase_when_done=False,
    )
    return app.run()
