"""
W&W Agent CLI.

Inspired by the Claw Code CLI shape in the adjacent reference project:
- interactive REPL
- slash commands
- session status
- markdown rendering
- visible tool calls
- prompt history and completion

Run:
    python cli.py
    python cli.py prompt "What can you do?"
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Windows + Chinese locale uses a GBK console by default, which crashes when
# rich tries to write characters like '⏺' / '✓' / '└' through its legacy
# console renderer. Force UTF-8 on stdout/stderr before importing rich so its
# Console picks up the corrected encoding.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        import io as _io
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from prompt_rules import (
    CONCISE_RULE,
    LANGUAGE_RULE,
    NO_RAW_TOOL_MARKUP_RULE,
)

console = Console()


def setup_logging() -> None:
    """Configure logging for the CLI application."""
    import agent_paths

    log_file = agent_paths.log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stderr) if os.getenv("LANGCHAIN_AGENT_DEBUG") else logging.NullHandler(),
        ],
    )

    # Suppress verbose third-party logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


SYSTEM_PROMPT_TEMPLATE = """\
You are W&W Agent CLI, a practical coding and research assistant running
inside a terminal. Your backend model is "{{model}}" via the "{{protocol}}"
protocol.

## Tool-use rules
- Use tools only when necessary for accuracy or to perform an action the
  user requested. Don't call a tool to make an answer feel more complete.
- Don't call current_datetime for greetings, identity questions, or
  ordinary chat — only when the user explicitly asks for the current date,
  time, timestamp, today, now, or a time-sensitive fact.
- Don't use run_python to reformat / sort / summarize data already in the
  conversation. Do that reasoning directly in the final answer.
- After using tools, the final answer must include the concrete findings
  from those tools. Don't claim you already provided details unless they
  are visible in the final answer.
- On tool error / empty result / missing token / nonzero exit / permission
  refusal: name what failed and what the user needs to do next.
- {no_raw_markup}

## Output style
- {concise}
- {language}
""".format(
    no_raw_markup=NO_RAW_TOOL_MARKUP_RULE,
    concise=CONCISE_RULE,
    language=LANGUAGE_RULE,
)

# Minimum number of buffered characters before the first stream flush.
STREAM_BUFFER_FLUSH_THRESHOLD = 24

# Minimum seconds to keep a tool-call label visible in the spinner.
MIN_TOOL_LABEL_SECONDS = 1.0

# Maximum characters shown in a tool result panel before truncation.
TOOL_RESULT_DISPLAY_LIMIT = 1200


SLASH_COMMANDS = {
    "/help": "Show available commands",
    "/status": "Show current session status",
    "/model": "Configure model interactively (provider → model → key → URL)",
    "/tools": "List registered LangChain tools",
    "/skills": "List installed local skills",
    "/instructions": "List loaded project instruction files",
    "/permissions": "Show or set permission mode",
    "/config": "Show effective local configuration",
    "/compact": "Start a fresh memory thread for later turns",
    "/clear": "Clear the terminal",
    "/exit": "Exit the CLI",
    "/quit": "Exit the CLI",
}


class Spinner:
    """Drop-in replacement for the old threaded Spinner.

    The previous implementation ran a daemon thread that wrote raw ANSI
    escape sequences (``\\033[36m{frame}\\033[0m``) directly to
    ``sys.stdout``. When a Rich ``Live`` region was also rendering to the
    same console, the two output streams fought each other and produced
    visible escape codes and duplicated lines in the user's terminal.

    This shim delegates to Rich's own ``Console.status()`` so the spinner
    composes cleanly with Live regions and Rich panels. The public API
    is unchanged (``start``, ``stop``, ``set_label``,
    ``set_thinking_after_tool_minimum``) so call sites stay intact.
    """

    MIN_TOOL_LABEL_SECONDS = MIN_TOOL_LABEL_SECONDS

    def __init__(self, label: str) -> None:
        self.label = label
        self._status = None
        self._stopped = False
        self._tool_label_started_at: float | None = None

    def start(self) -> None:
        if self._status is not None:
            return
        self._stopped = False
        self._status = console.status(self.label, spinner="dots")
        self._status.start()

    def set_label(self, label: str) -> None:
        if self._stopped:
            return
        self.label = label
        if label.startswith("Calling tool:"):
            self._tool_label_started_at = time.monotonic()
        else:
            self._tool_label_started_at = None
        if self._status is not None:
            self._status.update(status=label)

    def set_thinking_after_tool_minimum(self) -> None:
        remaining = 0.0
        if self._tool_label_started_at is not None:
            elapsed = time.monotonic() - self._tool_label_started_at
            remaining = max(0.0, self.MIN_TOOL_LABEL_SECONDS - elapsed)
        if remaining:
            time.sleep(remaining)
        self.set_label("Thinking...")

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._status is not None:
            self._status.stop()
            self._status = None


def can_use_interactive_picker() -> bool:
    """Whether the arrow-key picker can run in the current environment."""
    return sys.stdin.isatty() and sys.stdout.isatty()


# Lines reserved for the visible viewport inside the picker (excluding
# scroll indicators above/below).
_PICKER_VIEWPORT_ROWS = 18


def interactive_select(
    title: str,
    options: list[tuple[str, str]],
    default_index: int = 0,
    instruction: str = "↑/↓ move · space/enter confirm · esc/q cancel",
) -> int | None:
    """Inline arrow-key picker built on prompt_toolkit.

    ``options`` is a list of ``(primary, secondary)`` rows; ``secondary`` may
    be empty. Returns the chosen index, or ``None`` when the user pressed
    Esc/q/Ctrl+C. Callers must check :func:`can_use_interactive_picker` first;
    invoking this without a TTY raises ``RuntimeError``.

    The viewport scrolls to keep the cursor visible and displays
    ``↑ N more above`` / ``↓ N more below`` when the list is taller than the
    viewport.
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
                lines.append(("class:hint", f"   ↑ {start} more above\n"))
            else:
                lines.append(("", "\n"))

        for i in range(start, end):
            primary, secondary = options[i]
            if i == cursor[0]:
                marker = "▶ "
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
                lines.append(("class:hint", f"   ↓ {remaining} more below\n"))
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
    layout = Layout(HSplit([
        Window(content=FormattedTextControl(render_title), height=2),
        Window(content=FormattedTextControl(render_body), height=D(preferred=body_height, max=body_height)),
    ]))

    Application(layout=layout, key_bindings=kb, style=style, full_screen=False).run()
    return result[0]


@dataclass
class SessionState:
    provider: str = "<not loaded>"
    model: str = "<not loaded>"
    protocol: str = "<not loaded>"
    base_url: str = ""
    api_key_env: str = ""
    thread_id: str = "cli-session-1"
    turns: int = 0
    tool_calls: int = 0
    compacted_turns: int = 0
    seen_messages: int = 0
    last_error: str | None = None

    def compact(self) -> None:
        self.compacted_turns += self.turns
        self.turns = 0
        self.seen_messages = 0
        suffix = self.compacted_turns + 1
        self.thread_id = f"cli-session-{suffix}"


@dataclass
class CliApp:
    output_format: str = "text"
    state: SessionState = field(default_factory=SessionState)
    checkpointer: object | None = None

    def __post_init__(self) -> None:
        self.config_module, self.tools = self._load_runtime_modules()
        self.config_module.hydrate_env_from_credentials()
        self.skills = self._load_skills()
        self.instruction_files = self._load_instruction_files()
        self.project_settings = self._load_project_settings()
        self.active_cfg = self.config_module.load_active_config()
        self._apply_config(self.active_cfg)
        self._register_clarify_callback()
        self._memory_snapshot = self._load_memory_snapshot()
        # Stable system block (base + memory + instructions + skill catalog).
        # Computed lazily and cached so prompt caches downstream don't get
        # invalidated by per-turn changes — only the active-skills block
        # below changes between turns.
        self._cached_base_system_prompt: str | None = None
        self.agent = None

    @staticmethod
    def _load_memory_snapshot() -> str:
        """Freeze the MEMORY.md / USER.md state for system-prompt injection."""
        try:
            from tool import tool_memory

            return tool_memory.snapshot_for_system_prompt()
        except Exception as exc:
            logger.warning("memory snapshot load failed: %s", exc)
            return ""

    def _register_clarify_callback(self) -> None:
        """Bind the clarify tool to the CLI's interactive picker."""
        from tool import tool_clarify

        def ui_callback(question: str, choices: list[str] | None) -> str:
            console.print()
            console.print(Panel(question, title="Clarify", border_style="cyan", box=box.ROUNDED))
            if choices and can_use_interactive_picker():
                other_label = tool_clarify.get_other_label()
                rows = [(c, "") for c in choices] + [(other_label, "")]
                idx = interactive_select("Pick an answer:", rows, default_index=0)
                if idx is None:
                    return ""
                if idx == len(choices):
                    try:
                        return Prompt.ask("Your answer").strip()
                    except (EOFError, KeyboardInterrupt):
                        return ""
                return choices[idx]
            if choices:
                for i, choice in enumerate(choices, 1):
                    console.print(f"  {i}. {choice}")
                console.print(f"  {len(choices) + 1}. (type your own answer)")
                try:
                    raw = Prompt.ask("Choice (number or text)").strip()
                except (EOFError, KeyboardInterrupt):
                    return ""
                if raw.isdigit():
                    n = int(raw)
                    if 1 <= n <= len(choices):
                        return choices[n - 1]
                return raw
            try:
                return Prompt.ask("Your answer").strip()
            except (EOFError, KeyboardInterrupt):
                return ""

        tool_clarify.set_callback(ui_callback)

    def _apply_config(self, cfg) -> None:
        """Mirror an ActiveConfig into session state. Does not build the agent."""
        self.active_cfg = cfg
        self.state.provider = cfg.provider
        self.state.model = cfg.model
        self.state.protocol = cfg.protocol
        self.state.base_url = cfg.base_url
        self.state.api_key_env = cfg.api_key_env

    def _load_runtime_modules(self):
        try:
            import config
            from tool import tools
        except ModuleNotFoundError as exc:
            missing = exc.name or "dependency"
            raise RuntimeError(
                f"Missing Python dependency: {missing}. "
                "Install this project's requirements before running the agent."
            ) from exc
        return config, tools.ALL_TOOLS

    def _load_skills(self):
        from skills.skill_loader import load_skills

        return load_skills()

    def _load_instruction_files(self):
        from project_context import discover_instruction_files

        return discover_instruction_files()

    def _load_project_settings(self):
        from project_context import load_project_settings

        return load_project_settings()

    def _build_agent(self):
        from langchain_core.messages import HumanMessage, SystemMessage
        from langgraph.checkpoint.memory import MemorySaver

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="create_react_agent has been moved.*",
            )
            from langgraph.prebuilt import create_react_agent

        config = self.config_module
        self.checkpointer = MemorySaver()
        llm = config.build_llm(self.active_cfg)
        self.llm = llm

        def prompt_for_state(state):
            messages = state.get("messages", [])
            latest_user_text = ""
            for message in reversed(messages):
                if isinstance(message, HumanMessage):
                    latest_user_text = str(message.content)
                    break
            # Split the system block in two so the stable prefix (base +
            # memory + instructions + skill catalog) can be prompt-cached
            # while only the per-turn active-skills block changes.
            base_block = self.base_system_prompt()
            active_skills = self.active_skills_prompt(latest_user_text)
            system_msgs = [SystemMessage(content=base_block)]
            if active_skills:
                system_msgs.append(SystemMessage(content=active_skills))
            return system_msgs + messages

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="create_react_agent has been moved.*",
            )
            return create_react_agent(
                model=llm,
                tools=self.tools,
                prompt=prompt_for_state,
                checkpointer=self.checkpointer,
            )

    def base_system_prompt(self) -> str:
        """Stable per-session system block (base rules + memory + instructions + skill catalog).

        Excludes per-turn active skills so the same string is produced every
        turn. That string is eligible for the model's prompt cache; changing
        the content per turn would invalidate the cache on every request.
        """
        if self._cached_base_system_prompt is not None:
            return self._cached_base_system_prompt

        from skills.skill_loader import render_skill_catalog_for_prompt

        base_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            model=self.state.model,
            protocol=self.state.protocol,
        )
        catalog_prompt = render_skill_catalog_for_prompt(self.skills)
        instructions_prompt = self.render_project_instructions()
        memory_prompt = self._memory_snapshot
        sections = "\n\n".join(
            part
            for part in (memory_prompt, instructions_prompt, catalog_prompt)
            if part
        )
        self._cached_base_system_prompt = (
            base_prompt if not sections else f"{base_prompt}\n\n{sections}"
        )
        return self._cached_base_system_prompt

    def active_skills_prompt(self, user_text: str) -> str:
        """Per-turn block: skills matched by the latest user message.

        Empty when no skills match. Kept separate from
        :meth:`base_system_prompt` so prompt caching stays valid on stable
        turns.
        """
        from skills.skill_loader import (
            render_skills_for_prompt,
            select_skills_for_text,
        )

        matched = select_skills_for_text(self.skills, user_text)
        return render_skills_for_prompt(matched)

    def system_prompt(self, user_text: str = "") -> str:
        """Backwards-compat shim: full system block as one string.

        New call sites should prefer :meth:`base_system_prompt` +
        :meth:`active_skills_prompt` so the stable prefix can be cached.
        """
        base = self.base_system_prompt()
        active = self.active_skills_prompt(user_text)
        if not active:
            return base
        return f"{base}\n\n{active}"

    def render_project_instructions(self) -> str:
        from project_context import render_instruction_files

        return render_instruction_files(self.instruction_files)

    def run_repl(self) -> None:
        if not self.config_module.is_config_ready(self.active_cfg):
            self.run_setup_wizard(force=False)
        self.render_welcome()

        from prompt_toolkit.history import InMemoryHistory
        history = InMemoryHistory()

        while True:
            try:
                line = ask_boxed_input(history).strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye.[/dim]")
                return

            if not line:
                continue

            if self.handle_slash_command(line):
                continue

            self.state.turns += 1
            self.run_turn(line)
            console.print("[dim]" + "-" * 56 + "[/dim]")

    def run_prompt(self, prompt: str) -> None:
        if self.handle_slash_command(prompt):
            return
        if not self.config_module.is_config_ready(self.active_cfg):
            self.run_setup_wizard(force=False)
        self.state.turns += 1
        self.run_turn(prompt)

    def handle_slash_command(self, line: str) -> bool:
        command = line.split(maxsplit=1)[0].lower()
        if not command.startswith("/"):
            return False

        if command in ("/exit", "/quit"):
            raise SystemExit(0)
        if command == "/help":
            self.render_help()
            return True
        if command == "/status":
            self.render_status()
            return True
        if command == "/tools":
            self.render_tools()
            return True
        if command == "/skills":
            self.render_skills()
            return True
        if command == "/instructions":
            self.render_instructions()
            return True
        if command == "/permissions":
            self.handle_permissions_command(line)
            return True
        if command == "/model":
            self.handle_model_command(line)
            return True
        if command == "/config":
            self.render_config()
            return True
        if command == "/compact":
            self.state.compact()
            console.print(
                f"[green]Compacted local session.[/green] New thread: "
                f"[cyan]{self.state.thread_id}[/cyan]"
            )
            return True
        if command == "/clear":
            console.clear()
            return True

        console.print(f"[red]Unknown command:[/red] {command}")
        console.print("Type [cyan]/help[/cyan] for available commands.")
        return True

    def run_turn(self, question: str) -> None:
        from langchain_core.messages import HumanMessage, ToolMessage

        if self.agent is None:
            logger.info("Building agent (model=%s, protocol=%s)", self.state.model, self.state.protocol)
            self.agent = self._build_agent()

        logger.info("Turn %d: %s", self.state.turns, question[:120])

        config = {"configurable": {"thread_id": self.state.thread_id}}
        result = None
        local_seen_messages = self.state.seen_messages
        pending_tool_names: list[str] = []

        # Per-text-block state. Resets at each tool-call boundary so each
        # assistant text block becomes its own Live region.
        stream_buffer = ""
        suppressed_raw_stream = False
        any_output_printed = False

        spinner = Spinner("Thinking...")
        spinner.start()
        live: Live | None = None

        def start_live() -> None:
            nonlocal live
            if live is not None:
                return
            spinner.stop()
            initial = (
                Markdown(self.sanitize_for_console(stream_buffer))
                if stream_buffer.strip()
                else Text("")
            )
            live = Live(
                initial,
                console=console,
                refresh_per_second=8,
                transient=False,
            )
            live.start()

        def stop_live() -> None:
            nonlocal live
            if live is not None:
                try:
                    live.stop()
                except Exception:
                    pass
                live = None

        try:
            for event in self.agent.stream(
                {"messages": [HumanMessage(content=question)]},
                config=config,
                stream_mode=["messages", "values"],
            ):
                mode, payload = event if isinstance(event, tuple) and len(event) == 2 else ("values", event)

                if mode == "messages":
                    chunk, _metadata = payload
                    if self.is_tool_stream_chunk(chunk):
                        continue
                    if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
                        continue
                    token = self.message_chunk_text(chunk)
                    if not token:
                        continue
                    stream_buffer += token
                    if self.has_raw_tool_markup(stream_buffer):
                        suppressed_raw_stream = True
                        stream_buffer = ""
                        continue
                    if suppressed_raw_stream:
                        continue
                    start_live()
                    if live is not None:
                        live.update(Markdown(self.sanitize_for_console(stream_buffer)))
                        any_output_printed = True
                    continue

                if mode != "values":
                    continue
                state = payload
                result = state
                messages = state.get("messages", [])
                new_messages = messages[local_seen_messages:]
                local_seen_messages = len(messages)

                for msg in new_messages:
                    tool_calls = getattr(msg, "tool_calls", None)
                    if tool_calls:
                        self.state.tool_calls += len(tool_calls)
                        stop_live()
                        # Reset per-block stream state: the next assistant
                        # text block (after these tool results) is its own
                        # Live region.
                        stream_buffer = ""
                        suppressed_raw_stream = False
                        for call in tool_calls:
                            call_name = call.get("name", "tool")
                            call_args = call.get("args", {}) or {}
                            pending_tool_names.append(call_name)
                            self._render_tool_call_header(call_name, call_args)
                            any_output_printed = True
                        spinner = Spinner(f"Calling tool: {', '.join(pending_tool_names)}…")
                        spinner.start()
                    if isinstance(msg, ToolMessage):
                        spinner.stop()
                        completed_name = getattr(msg, "name", None) or "tool"
                        if completed_name in pending_tool_names:
                            pending_tool_names.remove(completed_name)
                        elif pending_tool_names:
                            pending_tool_names.pop(0)
                        self._render_tool_result_compact(msg)
                        any_output_printed = True
                        if pending_tool_names:
                            spinner = Spinner(f"Calling tool: {', '.join(pending_tool_names)}…")
                            spinner.start()
                        else:
                            spinner = Spinner("Thinking...")
                            spinner.start()

            stop_live()
            spinner.stop()

            if result is None:
                result = self.agent.invoke(
                    {"messages": [HumanMessage(content=question)]},
                    config=config,
                )
                local_seen_messages = len(result.get("messages", []))
            self.state.seen_messages = local_seen_messages
            self.state.last_error = None
        except Exception as exc:  # pragma: no cover - user-facing safety net
            stop_live()
            self.state.last_error = str(exc)
            logger.exception("Agent error on turn %d", self.state.turns)
            try:
                spinner.stop()
            except Exception:
                pass
            console.print(Panel(str(exc), title="Agent Error", border_style="red"))
            return
        finally:
            stop_live()
            try:
                spinner.stop()
            except Exception:
                pass

        messages = result.get("messages", [])
        if not any_output_printed:
            self.render_final_answer(messages, question)
        else:
            console.print()

    def render_trace(self, messages: Iterable[object]) -> None:
        from langchain_core.messages import ToolMessage

        for msg in messages:
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                self.state.tool_calls += len(tool_calls)
            if isinstance(msg, ToolMessage):
                continue

    def render_tool_completion(self, msg: object) -> None:
        name = getattr(msg, "name", None) or "tool"
        content = getattr(msg, "content", "") or ""
        if self.tool_result_looks_error(content):
            console.print(f"[red]Tool failed:[/red] {name}")
        else:
            console.print(f"[dim]Tool completed:[/dim] [green]{name}[/green]")

    def render_tool_call(self, name: str, args: dict) -> None:
        detail = ", ".join(f"{key}={value!r}" for key, value in args.items())
        title = f"Tool: {name}"
        body = detail if detail else "(no arguments)"
        console.print(Panel(body, title=title, border_style="cyan", box=box.ROUNDED))

    def render_tool_result(self, content: str) -> None:
        content = content or "(empty)"
        if len(content) > TOOL_RESULT_DISPLAY_LIMIT:
            content = content[:TOOL_RESULT_DISPLAY_LIMIT] + "\n... (truncated)"
        console.print(Panel(content, title="Tool Result", border_style="green", box=box.ROUNDED))

    # ------------------------------------------------------------------ #
    # Inline tool-call rendering (Claude-Code-style headers + previews).
    # Used by run_turn while the agent is streaming.
    # ------------------------------------------------------------------ #
    TOOL_ARG_PRIMARY_KEY = {
        "run_command": "command",
        "run_python": "code",
        "read_file": "path",
        "write_file": "path",
        "edit_file": "path",
        "apply_patch": "patch",
        "glob_search": "pattern",
        "grep_search": "pattern",
        "web_search": "query",
        "web_extract": "url",
        "list_directory": "path",
        "memory": "operation",
        "clarify": "question",
    }

    def _format_tool_args(self, name: str, args: dict) -> str:
        if not args:
            return ""
        key = self.TOOL_ARG_PRIMARY_KEY.get(name)
        if key and key in args and args[key] is not None:
            value = str(args[key])
            first_line = value.strip().splitlines()[0] if value.strip() else ""
            if len(first_line) > 80:
                first_line = first_line[:77] + "…"
            return first_line
        pairs = []
        for k, v in list(args.items())[:2]:
            v_str = v if isinstance(v, str) else repr(v)
            if len(v_str) > 40:
                v_str = v_str[:37] + "…"
            pairs.append(f"{k}={v_str}")
        return ", ".join(pairs)

    def _render_tool_call_header(self, name: str, args: dict) -> None:
        summary = self._format_tool_args(name, args)
        header = Text()
        header.append("⏺ ", style="bold cyan")
        header.append(name, style="bold")
        if summary:
            header.append(f"  {summary}", style="dim")
        console.print()
        console.print(header)

    def _render_tool_result_compact(self, msg: object) -> None:
        name = getattr(msg, "name", None) or "tool"
        content = str(getattr(msg, "content", "") or "")
        is_error = self.tool_result_looks_error(content)

        if name == "todo_write" and not is_error:
            self._render_todo_table(content)
            return
        if name in {"apply_patch", "edit_file", "write_file"} and not is_error:
            if self._render_diff_for_tool(name, content):
                return
        self._render_text_result(content, is_error=is_error)

    def _render_text_result(self, content: str, is_error: bool = False) -> None:
        MAX_LINES = 12
        lines = content.splitlines() if content else ["(empty)"]
        truncated_lines = max(0, len(lines) - MAX_LINES)
        shown = lines[:MAX_LINES]
        bullet_style = "red" if is_error else "green"
        bullet = Text("  └ ", style=bullet_style)
        body_lines = []
        for i, ln in enumerate(shown):
            prefix = bullet if i == 0 else Text("    ", style="dim")
            line = Text()
            line.append_text(prefix)
            line.append(ln, style="red" if is_error else "dim")
            body_lines.append(line)
        for ln in body_lines:
            console.print(ln)
        if truncated_lines:
            console.print(f"    [dim]… {truncated_lines} more line(s) collapsed[/dim]")

    def _render_diff_for_tool(self, name: str, content: str) -> bool:
        """Return True if a diff was rendered; False to fall back to text result."""
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(data, dict):
            return False

        diff_text = ""
        path_label = ""
        if name == "apply_patch":
            diff_text = str(data.get("diff") or "")
            paths = (
                list(data.get("filesModified") or [])
                + list(data.get("filesCreated") or [])
                + list(data.get("filesDeleted") or [])
            )
            path_label = ", ".join(paths)
        else:
            patch = data.get("structuredPatch")
            if isinstance(patch, list):
                diff_text = "\n".join(str(line) for line in patch)
            path_label = str(data.get("filePath") or "")

        if not diff_text.strip():
            console.print(f"  [green]└[/green] [dim]{path_label or '(no changes)'}[/dim]")
            return True

        diff_lines = diff_text.splitlines()
        MAX = 40
        truncated_lines = max(0, len(diff_lines) - MAX)
        shown = "\n".join(diff_lines[:MAX])
        header = Text("  └ ", style="green")
        if path_label:
            header.append(path_label, style="dim")
        console.print(header)
        syntax = Syntax(
            shown,
            "diff",
            theme="ansi_dark",
            line_numbers=False,
            background_color="default",
            word_wrap=False,
        )
        console.print(Padding(syntax, (0, 0, 0, 4)))
        if truncated_lines:
            console.print(f"    [dim]… {truncated_lines} more diff line(s) collapsed[/dim]")
        return True

    def _render_todo_table(self, content: str) -> None:
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        todos = data.get("todos") if isinstance(data, dict) else None
        if not isinstance(todos, list) or not todos:
            return
        table = Table(box=box.SIMPLE, show_header=False, pad_edge=False, padding=(0, 1))
        table.add_column(width=2, justify="center")
        table.add_column(overflow="fold")
        icons = {
            "completed": "[green]✓[/green]",
            "in_progress": "[yellow]⏳[/yellow]",
            "pending": "[dim]○[/dim]",
        }
        for todo in todos:
            if not isinstance(todo, dict):
                continue
            status = todo.get("status", "pending")
            if status == "in_progress":
                text = todo.get("activeForm") or todo.get("content") or "(unnamed)"
                text = f"[bold]{text}[/bold]"
            elif status == "completed":
                text = todo.get("content") or "(unnamed)"
                text = f"[dim strike]{text}[/dim strike]"
            else:
                text = todo.get("content") or "(unnamed)"
                text = f"[dim]{text}[/dim]"
            table.add_row(icons.get(status, "○"), text)
        console.print(Padding(table, (0, 0, 0, 2)))

    @staticmethod
    def tool_result_looks_error(content: str) -> bool:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                if parsed.get("errno") not in (None, 0):
                    return True
                if parsed.get("exitCode") not in (None, 0):
                    return True
                if parsed.get("interrupted"):
                    return True
                if parsed.get("returnCodeInterpretation"):
                    return True
                stderr = str(parsed.get("stderr") or "").strip()
                if stderr:
                    return True
        except Exception:
            pass

        lowered = content.lower()
        return any(
            marker in lowered
            for marker in (
                "error:",
                "permission denied",
                "traceback",
                "exception",
                "failed",
                "timed out",
            )
        )

    def render_final_answer(self, messages: list[object], question: str = "") -> None:
        if not messages:
            return
        last = messages[-1]
        content = getattr(last, "content", None)
        if not content:
            content = self.safe_reasoning_fallback(last)
        if not content:
            return
        raw_content = str(content)
        if self.has_raw_tool_markup(raw_content):
            recovered = self.recover_answer_from_tool_results(messages, question)
            content = recovered or "The model returned an unresolved tool call and I could not recover a final answer from the available tool results."
        else:
            content = self.strip_raw_tool_markup(raw_content)
        if not content.strip():
            console.print()
            console.print("The model returned an unresolved tool call and I could not recover a final answer from the available tool results.")
            console.print()
            return
        content = self.sanitize_for_console(str(content))
        console.print()
        console.print(Markdown(content))
        console.print()

    @staticmethod
    def sanitize_for_console(content: str) -> str:
        encoding = sys.stdout.encoding or (getattr(console, "encoding", None) or "utf-8")
        return content.encode(encoding, errors="replace").decode(encoding, errors="replace")

    @staticmethod
    def message_chunk_text(chunk: object) -> str:
        content = getattr(chunk, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""

    @staticmethod
    def is_tool_stream_chunk(chunk: object) -> bool:
        chunk_type = getattr(chunk, "type", "")
        if chunk_type in {"tool", "ToolMessage", "tool_message"}:
            return True
        class_name = chunk.__class__.__name__.lower()
        return "tool" in class_name

    @staticmethod
    def strip_raw_tool_markup(content: str) -> str:
        markers = ("<tool_call>", "<function=", "<parameter=")
        positions = [content.find(marker) for marker in markers if marker in content]
        if not positions:
            return content
        prefix = content[: min(positions)].strip()
        internal_markers = ("让我整理", "我需要按照", "搜索结果返回")
        if any(marker in prefix for marker in internal_markers):
            return ""
        return prefix

    @staticmethod
    def safe_reasoning_fallback(message: object) -> str:
        reasoning = getattr(message, "additional_kwargs", {}).get("reasoning_content", "")
        if not reasoning:
            return ""
        if CliApp.has_raw_tool_markup(reasoning):
            return ""
        internal_markers = ("让我整理", "我需要按照", "搜索结果返回", "搜索结果数据")
        if any(marker in reasoning for marker in internal_markers):
            return ""
        return reasoning

    @staticmethod
    def has_raw_tool_markup(content: str) -> bool:
        return any(marker in content for marker in ("<tool_call>", "<function=", "<parameter="))

    def recover_answer_from_tool_results(self, messages: list[object], question: str) -> str | None:
        return self.repair_raw_tool_answer(messages, question)

    def repair_raw_tool_answer(self, messages: list[object], question: str) -> str | None:
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

        llm = getattr(self, "llm", None)
        if llm is None:
            return None

        tool_summaries: list[str] = []
        for msg in messages[-12:]:
            if isinstance(msg, ToolMessage):
                name = getattr(msg, "name", "tool")
                content = str(getattr(msg, "content", "") or "")
                tool_summaries.append(f"{name} result:\n{content[:8000]}")

        if not tool_summaries:
            return None

        repair_messages = [
            SystemMessage(
                content=(
                    "You are repairing a final answer for a CLI agent. Do not call tools. "
                    f"{NO_RAW_TOOL_MARKUP_RULE} "
                    "Summarize the already-available tool results into a direct answer. "
                    "If a tool result contains an error, lead with the error and the next step. "
                    f"{LANGUAGE_RULE}"
                )
            ),
            HumanMessage(
                content=(
                    f"User question: {question}\n\n"
                    "Tool results already gathered:\n\n"
                    + "\n\n".join(tool_summaries)
                    + "\n\nReturn ONLY the final answer for the user."
                )
            ),
        ]
        try:
            response = llm.invoke(repair_messages)
        except Exception:
            return None
        content = str(getattr(response, "content", "") or "")
        return self.strip_raw_tool_markup(content).strip() or None

    def render_welcome(self) -> None:
        title = Text("W&W Agent CLI", style="bold cyan")
        subtitle = (
            f"Provider: {self.state.provider} | "
            f"Model: {self.state.model} | Protocol: {self.state.protocol} | "
            f"Tools: {len(self.tools)} | "
            f"Skills: {len(self.skills)} | "
            f"Instructions: {len(self.instruction_files)} | "
            "Type /help for commands"
        )
        console.print()
        console.print(Panel(subtitle, title=title, border_style="cyan", box=box.ROUNDED))
        console.print("[dim]Enter sends. Ctrl+J inserts a newline. Ctrl+L clears.[/dim]")
        console.print()

    def render_help(self) -> None:
        table = Table(title="Slash Commands", box=box.SIMPLE_HEAVY)
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description")
        for command, description in SLASH_COMMANDS.items():
            table.add_row(command, description)
        console.print(table)

    def render_status(self) -> None:
        table = Table(title="Session Status", box=box.SIMPLE_HEAVY)
        table.add_column("Field", style="cyan", no_wrap=True)
        table.add_column("Value")
        table.add_row("provider", self.state.provider)
        table.add_row("model", self.state.model)
        table.add_row("protocol", self.state.protocol)
        table.add_row("base_url", self.state.base_url)
        table.add_row("api_key_env", self.state.api_key_env)
        table.add_row("thread", self.state.thread_id)
        table.add_row("turns", str(self.state.turns))
        table.add_row("tool calls", str(self.state.tool_calls))
        table.add_row("skills", str(len(self.skills)))
        table.add_row("instructions", str(len(self.instruction_files)))
        table.add_row("permission mode", self.current_permission_mode())
        table.add_row("compacted turns", str(self.state.compacted_turns))
        table.add_row("last error", self.state.last_error or "<none>")
        console.print(table)

    def render_tools(self) -> None:
        table = Table(title="Registered Tools", box=box.SIMPLE_HEAVY)
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Description")
        for tool in self.tools:
            description = (getattr(tool, "description", "") or "").strip().splitlines()
            table.add_row(tool.name, description[0] if description else "")
        console.print(table)

    def render_skills(self) -> None:
        table = Table(title="Installed Skills", box=box.SIMPLE_HEAVY)
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Title")
        table.add_column("Path")
        if not self.skills:
            table.add_row("<none>", "", "")
        for skill in self.skills:
            table.add_row(skill.name, skill.title, str(skill.path))
        console.print(table)

    def render_instructions(self) -> None:
        table = Table(title="Project Instructions", box=box.SIMPLE_HEAVY)
        table.add_column("Path", style="cyan")
        table.add_column("Characters", justify="right")
        if not self.instruction_files:
            table.add_row("<none>", "0")
        for file in self.instruction_files:
            table.add_row(str(file.path), str(len(file.content)))
        console.print(table)

    def run_setup_wizard(self, force: bool = False) -> bool:
        """Alias for the model wizard, kept for ``/setup`` and startup checks."""
        config = self.config_module
        if not force and config.is_config_ready(self.active_cfg):
            return True
        return self.run_model_wizard()

    def handle_model_command(self, line: str) -> None:
        """`/model` launches the 4-step provider+model+key+url wizard.

        `/model <provider>` skips Step 1 and uses the named provider directly.
        """
        config = self.config_module
        parts = line.split(maxsplit=1)
        provider_hint = parts[1].strip() if len(parts) > 1 else ""
        if provider_hint and provider_hint not in config.PROVIDERS:
            console.print(f"[red]Unknown provider:[/red] {provider_hint}")
            console.print("Run [cyan]/model[/cyan] to choose interactively.")
            return
        self.run_model_wizard(provider_hint=provider_hint or None)

    # ------------------------------------------------------------------
    # Interactive 4-step wizard (modeled on hermes-agent's `hermes model`)
    # ------------------------------------------------------------------
    def run_model_wizard(self, provider_hint: str | None = None) -> bool:
        config = self.config_module
        console.print()
        console.print(Panel(
            "Configure the active model in four steps:\n"
            "  1. Select provider\n"
            "  2. Select model\n"
            "  3. Enter API key\n"
            "  4. Enter base URL\n"
            "\n"
            "[dim]Picker controls: ↑/↓ move · space/enter confirm · esc/q cancel[/dim]",
            title="Model Configuration",
            border_style="cyan",
            box=box.ROUNDED,
        ))

        provider_name = provider_hint or self._wizard_select_provider()
        if not provider_name:
            console.print("[yellow]Cancelled.[/yellow]")
            return False

        provider = config.PROVIDERS[provider_name]
        is_custom = provider_name == "custom" or not provider.get("models")

        model = self._wizard_select_model(provider_name, provider, is_custom)
        if not model:
            console.print("[yellow]Cancelled — no model selected.[/yellow]")
            return False

        api_key_env = provider.get("api_key_env") or "CUSTOM_API_KEY"
        api_key = self._wizard_enter_api_key(api_key_env)
        if not api_key:
            console.print("[yellow]Cancelled — API key required.[/yellow]")
            return False

        base_url = self._wizard_enter_base_url(provider.get("base_url", ""), is_custom)
        if not base_url:
            console.print("[yellow]Cancelled — base URL required.[/yellow]")
            return False

        new_cfg = config.make_config(
            provider=provider_name,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
        )

        try:
            config.save_credential(api_key_env, api_key)
        except OSError as exc:
            console.print(f"[red]Failed to save credential:[/red] {exc}")
            return False
        os.environ[api_key_env] = api_key

        try:
            config.save_active_config(new_cfg)
        except OSError as exc:
            console.print(f"[yellow]Switched in memory only — failed to persist:[/yellow] {exc}")

        self._apply_config(new_cfg)
        self.agent = None
        self.state.seen_messages = 0
        console.print()
        console.print(
            f"[green]Active model:[/green] [cyan]{new_cfg.provider}[/cyan] / "
            f"[cyan]{new_cfg.model}[/cyan]  "
            f"[dim]({new_cfg.protocol} @ {new_cfg.base_url})[/dim]"
        )
        return True

    def _wizard_select_provider(self) -> str:
        config = self.config_module
        provider_names = config.list_providers()

        rows: list[tuple[str, str]] = []
        for name in provider_names:
            provider = config.PROVIDERS[name]
            env_name = provider.get("api_key_env", "")
            has_key = bool(env_name and (os.getenv(env_name) or env_name in config.load_credentials()))
            mark = "●" if has_key else "○"
            primary = f"{mark} {name:<22s} {provider.get('label', '')}"
            secondary = f"[{provider['protocol']:>9s}]  key={env_name}"
            rows.append((primary, secondary))

        try:
            default_idx = provider_names.index(self.state.provider)
        except ValueError:
            default_idx = 0

        console.print()
        console.print("[bold cyan]Step 1 / 4 — Select provider[/bold cyan]")
        console.print("[dim]● = API key already set    ○ = needs key[/dim]")

        if not can_use_interactive_picker():
            return self._fallback_text_choice(
                "Provider name",
                provider_names,
                default=provider_names[default_idx],
            )
        idx = interactive_select(
            "Select a model provider:",
            rows,
            default_index=default_idx,
        )
        if idx is None:
            return ""
        return provider_names[idx]

    def _wizard_select_model(self, provider_name: str, provider: dict, is_custom: bool) -> str:
        models = list(provider.get("models") or [])

        if is_custom or not models:
            console.print()
            console.print("[bold cyan]Step 2 / 4 — Enter model name[/bold cyan]")
            default = self.state.model if self.state.provider == provider_name else ""
            try:
                return Prompt.ask(
                    "Model id (e.g. llama-3-70b, gpt-4o)",
                    default=default or None,
                ).strip()
            except (EOFError, KeyboardInterrupt):
                return ""

        try:
            default_idx = models.index(self.state.model) if self.state.model in models else 0
        except ValueError:
            default_idx = 0

        console.print()
        console.print(f"[bold cyan]Step 2 / 4 — Select model from {provider_name}[/bold cyan]")

        if not can_use_interactive_picker():
            choice = self._fallback_text_choice(
                "Model id",
                models,
                default=models[default_idx],
            )
            return choice  # text fallback may also accept a free-form model id

        OTHER = "+ Enter a model name not listed..."
        rows = [(m, "") for m in models] + [(OTHER, "")]
        idx = interactive_select(
            "Select a model:",
            rows,
            default_index=default_idx,
        )
        if idx is None:
            return ""
        if idx == len(models):
            try:
                return Prompt.ask("Model id").strip()
            except (EOFError, KeyboardInterrupt):
                return ""
        return models[idx]

    def _fallback_text_choice(self, label: str, choices: list[str], default: str) -> str:
        """Numbered text prompt used when interactive picker isn't available."""
        for i, c in enumerate(choices, 1):
            marker = " *" if c == default else "  "
            console.print(f"  {i:>2}.{marker} {c}")
        try:
            raw = Prompt.ask(f"{label} (number or name)", default=default)
        except (EOFError, KeyboardInterrupt):
            return ""
        raw = raw.strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        return raw

    def _wizard_enter_api_key(self, env_name: str) -> str:
        config = self.config_module
        console.print()
        console.print(f"[bold]Step 3 / 4 — Enter API key[/bold] ([cyan]{env_name}[/cyan])")
        existing = os.getenv(env_name) or config.load_credentials().get(env_name, "")
        if existing:
            masked = existing[:6] + "..." if len(existing) > 6 else "***"
            try:
                keep = Prompt.ask(
                    f"Key already set ({masked}). Keep it?",
                    choices=["y", "n"],
                    default="y",
                )
            except (EOFError, KeyboardInterrupt):
                return ""
            if keep == "y":
                return existing

        try:
            entered = Prompt.ask(f"{env_name}", password=True).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        return entered

    def _wizard_enter_base_url(self, default_url: str, is_custom: bool) -> str:
        console.print()
        console.print("[bold]Step 4 / 4 — Enter base URL[/bold]")
        if not is_custom and default_url:
            try:
                url = Prompt.ask("Base URL", default=default_url).strip()
            except (EOFError, KeyboardInterrupt):
                return ""
        else:
            try:
                url = Prompt.ask(
                    "Base URL (e.g. https://api.example.com/v1)",
                    default=default_url or None,
                ).strip()
            except (EOFError, KeyboardInterrupt):
                return ""

        if not url:
            return ""
        if not url.startswith(("http://", "https://")):
            console.print(
                f"[red]Invalid URL[/red] {url!r} — must start with http:// or https://"
            )
            return ""
        return url

    def handle_permissions_command(self, line: str) -> None:
        parts = line.split(maxsplit=1)
        if len(parts) == 1:
            console.print(f"Current permission mode: [cyan]{self.current_permission_mode()}[/cyan]")
            console.print("Use [cyan]/permissions read-only|workspace-write|danger-full-access[/cyan] to switch.")
            return
        requested = parts[1].strip()
        if requested not in {"read-only", "workspace-write", "danger-full-access"}:
            console.print("[red]Invalid permission mode.[/red] Use read-only, workspace-write, or danger-full-access.")
            return
        os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] = requested
        console.print(f"Permission mode set for this session: [cyan]{requested}[/cyan]")

    def current_permission_mode(self) -> str:
        from tool.tool_permissions import PermissionPolicy

        return PermissionPolicy.from_env().active_mode.label

    def render_config(self) -> None:
        table = Table(title="Effective Config", box=box.SIMPLE_HEAVY)
        table.add_column("Key", style="cyan", no_wrap=True)
        table.add_column("Value")
        cfg = self.active_cfg
        provider_meta = self.config_module.PROVIDERS.get(cfg.provider, {})
        table.add_row("provider", f"{cfg.provider} — {provider_meta.get('label', '')}")
        table.add_row("model", cfg.model)
        table.add_row("protocol", cfg.protocol)
        table.add_row("base_url", cfg.base_url)
        table.add_row("permission mode", self.current_permission_mode())
        env_name = cfg.api_key_env
        table.add_row(env_name, "<set>" if os.getenv(env_name) else "<missing>")
        table.add_row("LANGCHAIN_AGENT_MODEL", os.getenv("LANGCHAIN_AGENT_MODEL") or "<unset>")
        table.add_row("BAIDU_EC_SEARCH_TOKEN", "<set>" if os.getenv("BAIDU_EC_SEARCH_TOKEN") else "<missing>")
        table.add_row("project settings", json.dumps(self.project_settings, ensure_ascii=False) or "{}")
        if self.instruction_files:
            table.add_row("instruction files", "\n".join(str(file.path) for file in self.instruction_files))
        else:
            table.add_row("instruction files", "<none>")
        console.print(table)


def _make_slash_completer(commands: dict[str, str]):
    """Build a Completer subclass that yields slash commands with description meta.

    Subclassing prompt_toolkit's ``Completer`` is required so that
    ``get_completions_async`` (used when ``complete_while_typing=True``) is
    available via the base class. We build it inside a function so the
    import lives next to the construction site.
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


def ask_boxed_input(history, *, label: str = "") -> str:
    """Read a single user submission inside a bordered input box (Claude Code style).

    Each call builds a fresh non-fullscreen ``prompt_toolkit.Application`` whose
    layout is a ``Frame`` wrapping a multi-line input window. The application
    renders inline at the current cursor position, so:

    - When prior output is short, the box sits right after it (no padding).
    - When prior output already fills the screen, normal terminal scrolling
      pushes the box to the visible bottom on its own.
    - Slash-command completions float above the cursor via a ``CompletionsMenu``.

    Returns the submitted text. Raises ``KeyboardInterrupt`` on Ctrl+C and
    ``EOFError`` on Ctrl+D against an empty buffer.
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

    completer = _make_slash_completer(SLASH_COMMANDS)
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

    # Left-of-cursor "▌ " indicator inside the box.
    before_input = BeforeInput(text="▌ ", style="class:prompt-mark")
    input_control = BufferControl(
        buffer=buf,
        input_processors=[ConditionalProcessor(before_input, has_focus(buf))],
    )

    # Shrink-to-content height: number of newlines in the buffer + 1,
    # clamped between 1 and 8. Recomputed on every render so the frame
    # grows as the user types newlines and stays tight when empty.
    def _calc_input_height() -> Dimension:
        line_count = buf.text.count("\n") + 1
        rows = min(max(line_count, 1), 8)
        return Dimension.exact(rows)

    input_window = Window(
        content=input_control,
        wrap_lines=True,
        height=_calc_input_height,
    )

    framed = Frame(input_window, style="class:input-frame", title=f" {label} " if label else None)
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


def create_prompt_session():
    """Kept for tests / external callers. The REPL itself uses ``ask_boxed_input``."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("c-c")
    def _(event) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    @bindings.add("c-l")
    def _(event) -> None:
        console.clear()

    @bindings.add("enter")
    def _(event) -> None:
        buf = event.app.current_buffer
        # Backslash-continuation: trailing ` \` on the line becomes a newline
        # rather than submitting. Matches editor convention.
        text = buf.document.text
        if text.endswith("\\") and not text.endswith("\\\\"):
            buf.delete_before_cursor(1)
            buf.insert_text("\n")
            return
        buf.validate_and_handle()

    @bindings.add("c-j")
    def _(event) -> None:
        event.app.current_buffer.insert_text("\n")

    @bindings.add("escape", "enter")
    def _(event) -> None:
        event.app.current_buffer.insert_text("\n")

    return PromptSession(
        completer=_make_slash_completer(SLASH_COMMANDS),
        complete_while_typing=True,
        history=InMemoryHistory(),
        key_bindings=bindings,
        multiline=True,
        prompt_continuation=ANSI("\033[2m... \033[0m"),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="W&W Agent CLI")
    parser.add_argument(
        "--output-format",
        choices=("text",),
        default="text",
        help="Reserved for Claw-style output mode parity.",
    )
    subparsers = parser.add_subparsers(dest="command")
    prompt_parser = subparsers.add_parser("prompt", help="Run one prompt and exit")
    prompt_parser.add_argument("prompt", nargs="+")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    setup_logging()
    args = parse_args(argv or sys.argv[1:])
    try:
        app = CliApp(output_format=args.output_format)
    except RuntimeError as exc:
        logger.error("Startup error: %s", exc)
        console.print(Panel(str(exc), title="Startup Error", border_style="red"))
        console.print("Run [cyan]python -m pip install -r requirements.txt[/cyan] and try again.")
        raise SystemExit(1) from exc

    if args.command == "prompt":
        app.run_prompt(" ".join(args.prompt))
        return

    app.run_repl()


# Public API for cli.py dispatcher.
# Phase 2 of the multi-agent refactor expects these names.
def run_repl() -> int:
    setup_logging()
    try:
        app = CliApp(output_format="text")
    except RuntimeError as exc:
        logger.error("Startup error: %s", exc)
        console.print(Panel(str(exc), title="Startup Error", border_style="red"))
        console.print("Run [cyan]python -m pip install -r requirements.txt[/cyan] and try again.")
        return 1
    app.run_repl()
    return 0


def run_prompt(prompt: str) -> int:
    setup_logging()
    try:
        app = CliApp(output_format="text")
    except RuntimeError as exc:
        logger.error("Startup error: %s", exc)
        console.print(Panel(str(exc), title="Startup Error", border_style="red"))
        console.print("Run [cyan]python -m pip install -r requirements.txt[/cyan] and try again.")
        return 1
    app.run_prompt(prompt)
    return 0
