from __future__ import annotations

import asyncio
import sys
from typing import TextIO

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

COMMANDS: dict[str, str] = {
    "/help": "Show available commands",
    "/exit": "Exit the CLI",
    "/quit": "Exit the CLI",
    "/agents": "List multi-agent specialists",
    "/tools": "List registered specialist capabilities",
    "/permissions": "Show or set permission mode",
    "/config": "Show effective multi-agent configuration",
    "/model": "Configure model interactively",
    "/skills": "List installed local skills",
    "/instructions": "List loaded project instruction files",
    "/clear": "Clear the terminal",
    "/compact": "Start a fresh memory thread for later turns",
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
        self._prompt_session = None

    def _is_tty(self) -> bool:
        if self.input_stream is sys.stdin:
            return sys.stdin.isatty()
        return False

    # -- input --

    def read_input(self) -> str:
        if not self._is_tty():
            line = self.input_stream.readline()
            if line == "":
                raise EOFError
            return line.strip()
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.history import InMemoryHistory
        except ImportError:
            return input("multi-agent> ").strip()
        if self._prompt_session is None:
            self._prompt_session = PromptSession(
                history=InMemoryHistory(),
                completer=WordCompleter(list(COMMANDS), ignore_case=True),
            )
        return self._prompt_session.prompt("multi-agent> ").strip()

    async def read_input_async(self) -> str:
        if not self._is_tty():
            loop = asyncio.get_running_loop()
            line = await loop.run_in_executor(None, self.input_stream.readline)
            if line == "":
                raise EOFError
            return line.strip()
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.history import InMemoryHistory
        except ImportError:
            loop = asyncio.get_running_loop()
            return (await loop.run_in_executor(None, input, "multi-agent> ")).strip()
        if self._prompt_session is None:
            self._prompt_session = PromptSession(
                history=InMemoryHistory(),
                completer=WordCompleter(list(COMMANDS), ignore_case=True),
            )
        return (await self._prompt_session.prompt_async("multi-agent> ")).strip()

    # -- rendering --

    def render_welcome(
        self, *, provider: str, model: str, permission_mode: str,
        agent_count: int, workspace: str,
    ) -> None:
        subtitle = (
            f"Provider: {provider} | Model: {model} | "
            f"Permission: {permission_mode} | Agents: {agent_count} | Workspace: {workspace}"
        )
        self.console.print()
        self.console.print(Panel(
            subtitle,
            title=Text("LangChain Agent CLI — Multi-Agent", style="bold cyan"),
            border_style="cyan", box=box.ROUNDED,
        ))
        self.console.print("[dim]Enter sends. Type /help for commands.[/dim]")
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
        self.console.print(Panel(
            text or "<empty>", title=title, border_style=style, box=box.ROUNDED,
        ))

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
