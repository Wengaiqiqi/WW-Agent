from __future__ import annotations

import asyncio
import sys
from typing import TextIO

from prompt_toolkit.history import InMemoryHistory
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from orchestrator.ui_input import ask_boxed_input

COMMANDS: dict[str, str] = {
    "/help": "Show available commands",
    "/exit": "Exit the CLI",
    "/quit": "Exit the CLI",
    "/status": "Show current session status",
    "/agents": "List multi-agent specialists",
    "/tools": "List registered specialist capabilities",
    "/permissions": "Show or set permission mode",
    "/config": "Show effective multi-agent configuration",
    "/model": "Configure model interactively",
    "/skills": "List installed local skills",
    "/instructions": "List loaded project instruction files",
    "/clear": "Clear the terminal",
    "/compact": "Start a fresh memory thread for later turns",
    "/gateway": "Configure and run chat-platform gateways (Feishu, QQ, ...)",
}


class ReplUI:
    def __init__(
        self,
        *,
        console: Console | None = None,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ):
        self.input_stream = input_stream or sys.stdin
        self.output_stream = output_stream or sys.stdout
        self.console = console or Console(file=self.output_stream)
        self._active_label: str = "multi-agent"
        self.history = InMemoryHistory()

    def set_agent_context(self, label: str) -> None:
        """Switch the input prompt label (e.g. 'multi-agent' ↔ 'tool-agent')."""
        self._active_label = label

    # -- input --

    def read_input(self) -> str:
        if not self._is_tty():
            line = self.input_stream.readline()
            if line == "":
                raise EOFError
            return line.strip()
        return ask_boxed_input(
            self.history, label="", commands=COMMANDS, console=self.console,
        ).strip()

    async def read_input_async(self) -> str:
        if not self._is_tty():
            loop = asyncio.get_running_loop()
            line = await loop.run_in_executor(None, self.input_stream.readline)
            if line == "":
                raise EOFError
            return line.strip()
        loop = asyncio.get_running_loop()
        return (
            await loop.run_in_executor(
                None,
                lambda: ask_boxed_input(
                    self.history, label="", commands=COMMANDS, console=self.console,
                ).strip(),
            )
        )

    def _is_tty(self) -> bool:
        if self.input_stream is sys.stdin:
            return sys.stdin.isatty()
        return False

    # -- rendering --

    def render_welcome(
        self, *, provider: str, model: str, protocol: str,
        permission_mode: str, agent_count: int,
        tool_count: int = 0, skill_count: int = 0,
        instruction_count: int = 0, workspace: str = "",
    ) -> None:
        parts = [
            f"Provider: {provider}",
            f"Model: {model}",
            f"Protocol: {protocol}",
            f"Permission: {permission_mode}",
            f"Agents: {agent_count}",
        ]
        if tool_count:
            parts.append(f"Tools: {tool_count}")
        if skill_count:
            parts.append(f"Skills: {skill_count}")
        if instruction_count:
            parts.append(f"Instructions: {instruction_count}")
        parts.append("Type /help for commands")

        title = Text("W&W Agent CLI", style="bold cyan")
        subtitle = " | ".join(parts)
        self.console.print()
        self.console.print(Panel(subtitle, title=title, border_style="cyan", box=box.ROUNDED))
        self.console.print("[dim]Enter sends. Ctrl+J inserts a newline. Ctrl+L clears.[/dim]")
        self.console.print()

    def render_goodbye(self) -> None:
        self.console.print("[cyan]Goodbye.[/cyan]")

    def render_help(self) -> None:
        table = Table(title="Slash Commands", box=box.SIMPLE_HEAVY)
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description")
        for command, description in COMMANDS.items():
            table.add_row(command, description)
        self.console.print(table)

    def render_error(self, title: str, message: str) -> None:
        self.console.print(Panel(
            message, title=title, border_style="red", box=box.ROUNDED,
        ))

    def render_warning(self, message: str) -> None:
        self.console.print(Panel(
            message, title="Warning", border_style="yellow", box=box.ROUNDED,
        ))

    def render_text(self, *, title: str, text: str, style: str = "cyan") -> None:
        """Render an unboxed titled block — used for slash-command output.

        A bold colored title on its own line, content below. Slash commands
        deliberately do NOT use ``Panel`` here: the framed look is reserved
        for runtime state — errors, warnings, the welcome banner — so users
        can tell at a glance whether something needs their attention or is
        just informational output they asked for.
        """
        self.console.print()
        self.console.print(f"[bold {style}]{title}[/bold {style}]")
        if text:
            self.console.print(text)
        self.console.print()

    def render_command_error(self, title: str, message: str) -> None:
        """Render a slash-command error inline (no panel, red title + body).

        Distinct from ``render_error`` (which keeps the framed panel for
        runtime errors like turn failures / agent crashes) — slash command
        errors should look like the other slash output, just colored red.
        """
        self.console.print()
        self.console.print(f"[bold red]{title}[/bold red]")
        if message:
            self.console.print(f"[red]{message}[/red]")
        self.console.print()

    def render_markdown(self, text: str) -> None:
        """Render unboxed Markdown — used for LLM responses like --single mode."""
        from rich.markdown import Markdown
        self.console.print()
        self.console.print(Markdown(text or ""))
        self.console.print()

    def render_agent_label(self, agent_id: str) -> None:
        """Print a cyan `[agent-id]:` label above streamed content."""
        self.console.print()
        self.console.print(f"[bold cyan]\\[{agent_id}]:[/bold cyan]")

    def render_table(
        self, *, title: str, columns: list[str], rows: list[list[str]],
    ) -> None:
        table = Table(title=title, box=box.SIMPLE_HEAVY)
        for i, column in enumerate(columns):
            table.add_column(column, style="cyan" if i == 0 else "")
        if not rows:
            table.add_row("<none>", *["" for _ in columns[1:]])
        for row in rows:
            table.add_row(*row)
        self.console.print(table)

    def render_divider(self) -> None:
        self.console.print("[dim]" + "-" * 56 + "[/dim]")

    def render_cancelled(self) -> None:
        self.console.print("[yellow]Cancelled.[/yellow]")

    def clear(self) -> None:
        self.console.clear()
