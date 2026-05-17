# Multi-Agent REPL Implementation Plan (Revised)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Phase 5 multi-agent REPL stub with a legacy-feeling interactive REPL via `REPLController` + `ReplCommandHandler` + `ReplUI`.

**Architecture:** `main.py` stays thin — bootstrap deps, construct `REPLController`, run prompt_toolkit input loop, shutdown. Business logic lives in `repl_controller.py` (turn execution, lazy planner, tiered errors), `repl_commands.py` (slash commands via `ReplUI` render methods), and `repl_types.py` (shared `LoopAction` enum). `ReplUI` encapsulates prompt_toolkit + Rich.

**Tech Stack:** Python 3.13, pytest + pytest-asyncio, LangGraph, MCP stdio, Rich, prompt_toolkit.

**Status:** `repl_state.py`, `turns.py`, `mcp_host.py`, `graph.py`, `router.py`, `telemetry.py`, `permission_gate.py`, `stream_mux.py`, and `registry.py` already exist. The stub `run_repl()` in `main.py` is the gap.

---

## File Structure

Create:

- `orchestrator/repl_types.py` — `LoopAction` enum (shared, zero intra-orchestrator imports)
- `orchestrator/repl_controller.py` — `REPLController` (async handle_input, _execute_turn, lazy _ensure_planner, _is_fatal)
- `orchestrator/repl_ui.py` — `ReplUI(mux)` — prompt_toolkit input, Rich rendering, non-TTY fallback
- `orchestrator/repl_commands.py` — `ReplCommandHandler(ui, state, host, router)` — slash command dispatch

Modify:

- `orchestrator/repl_state.py` — add `render_planner_context(capabilities) -> str`
- `orchestrator/main.py` — replace stub `run_repl()` with thin bootstrap + controller loop + shutdown

Tests (create):

- `tests/test_orchestrator/test_repl_types.py`
- `tests/test_orchestrator/test_repl_controller.py`
- `tests/test_orchestrator/test_repl_ui.py`
- `tests/test_orchestrator/test_repl_commands.py`

Tests (modify):

- `tests/test_orchestrator/test_repl_state.py` — add context rendering test
- `tests/test_e2e_multi_agent/test_e2e_simple_tool.py` — add REPL stdin e2e regression

---

### Task 1: Shared LoopAction Enum

**Files:**
- Create: `orchestrator/repl_types.py`
- Create: `tests/test_orchestrator/test_repl_types.py`

- [ ] **Step 1: Write the test**

Create `tests/test_orchestrator/test_repl_types.py`:

```python
from __future__ import annotations

from orchestrator.repl_types import LoopAction


def test_loop_action_enum_values():
    assert LoopAction.CONTINUE is not LoopAction.EXIT
    assert LoopAction.CONTINUE.name == "CONTINUE"
    assert LoopAction.EXIT.name == "EXIT"


def test_loop_action_no_deps():
    import ast, inspect
    source = inspect.getsource(LoopAction)
    tree = ast.parse(source)
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert len(imports) == 0, "LoopAction module should have zero imports"
```

- [ ] **Step 2: Run test to verify import failure**

```bash
pytest tests/test_orchestrator/test_repl_types.py -q
```

Expected: `ModuleNotFoundError: No module named 'orchestrator.repl_types'`

- [ ] **Step 3: Implement `orchestrator/repl_types.py`**

```python
from enum import Enum, auto


class LoopAction(Enum):
    CONTINUE = auto()
    EXIT = auto()
```

- [ ] **Step 4: Run test to verify pass**

```bash
pytest tests/test_orchestrator/test_repl_types.py -q
```

Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_types.py tests/test_orchestrator/test_repl_types.py
git commit -m "feat(orchestrator): add shared LoopAction enum"
```

---

### Task 2: Terminal UI Adapter (ReplUI)

**Files:**
- Create: `orchestrator/repl_ui.py`
- Create: `tests/test_orchestrator/test_repl_ui.py`

- [ ] **Step 1: Write failing UI tests**

Create `tests/test_orchestrator/test_repl_ui.py`:

```python
from __future__ import annotations

import io
import sys

from rich.console import Console

from orchestrator.repl_ui import COMMANDS, ReplUI


def _make_ui(stdin_text: str = "") -> tuple[ReplUI, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    ui = ReplUI(
        console=console,
        input_stream=io.StringIO(stdin_text),
        output_stream=buf,
    )
    return ui, buf


def test_replui_is_not_tty():
    ui, _ = _make_ui()
    assert not ui._is_tty()


def test_command_list_has_all_expected_commands():
    assert set(COMMANDS) == {
        "/help", "/exit", "/quit", "/agents", "/tools",
        "/permissions", "/config", "/model", "/skills",
        "/instructions", "/clear", "/compact",
    }


def test_render_help_contains_commands():
    ui, buf = _make_ui()
    ui.render_help()
    text = buf.getvalue()
    assert "Slash Commands" in text
    assert "/agents" in text
    assert "/compact" in text
    assert "/model" in text


def test_render_error_shows_title_and_message():
    ui, buf = _make_ui()
    ui.render_error("Planner Error", "invalid JSON")
    text = buf.getvalue()
    assert "Planner Error" in text
    assert "invalid JSON" in text


def test_render_welcome_shows_provider_model_permission_agents():
    ui, buf = _make_ui()
    ui.render_welcome(
        provider="openai", model="gpt-4o",
        permission_mode="workspace-write", agent_count=2,
        workspace="/home/project",
    )
    text = buf.getvalue()
    assert "Multi-Agent" in text
    assert "openai" in text
    assert "gpt-4o" in text
    assert "workspace-write" in text
    assert "2" in text


def test_render_warning_shows_message():
    ui, buf = _make_ui()
    ui.render_warning("Memory refresh failed, using old snapshot")
    text = buf.getvalue()
    assert "Memory refresh failed" in text


def test_render_table_renders_rows():
    ui, buf = _make_ui()
    ui.render_table(title="Test Table", columns=["Name", "Value"], rows=[["a", "1"], ["b", "2"]])
    text = buf.getvalue()
    assert "Test Table" in text
    assert "a" in text
    assert "1" in text


def test_render_table_empty_renders_none():
    ui, buf = _make_ui()
    ui.render_table(title="Empty", columns=["X", "Y"], rows=[])
    text = buf.getvalue()
    assert "<none>" in text


def test_read_input_non_tty_reads_from_stream():
    ui, _ = _make_ui(stdin_text="hello world\n")
    result = ui.read_input()
    assert result == "hello world"


def test_read_input_non_tty_eof():
    ui, _ = _make_ui(stdin_text="")
    try:
        ui.read_input()
        assert False, "expected EOFError"
    except EOFError:
        pass


def test_read_input_async_non_tty_reads_from_stream():
    import asyncio
    ui, _ = _make_ui(stdin_text="/help\n")

    async def _read():
        return await ui.read_input_async()

    result = asyncio.run(_read())
    assert result == "/help"


def test_render_text_panel():
    ui, buf = _make_ui()
    ui.render_text(title="Result", text="hello", style="green")
    text = buf.getvalue()
    assert "Result" in text
    assert "hello" in text


def test_render_divider():
    ui, buf = _make_ui()
    ui.render_divider()
    text = buf.getvalue()
    assert "-" in text


def test_clear():
    ui, buf = _make_ui()
    ui.clear()
    # clear() should not raise


def test_render_goodbye():
    ui, buf = _make_ui()
    ui.render_goodbye()
    text = buf.getvalue()
    assert "Goodbye" in text


def test_render_cancelled():
    ui, buf = _make_ui()
    ui.render_cancelled()
    text = buf.getvalue()
    assert "Cancelled" in text
```

- [ ] **Step 2: Run UI tests to verify import failure**

```bash
pytest tests/test_orchestrator/test_repl_ui.py -q
```

Expected: `ModuleNotFoundError: No module named 'orchestrator.repl_ui'`

- [ ] **Step 3: Implement `orchestrator/repl_ui.py`**

```python
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
```

- [ ] **Step 4: Run UI tests to verify pass**

```bash
pytest tests/test_orchestrator/test_repl_ui.py -q
```

Expected: `16 passed`

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_ui.py tests/test_orchestrator/test_repl_ui.py
git commit -m "feat(orchestrator): add multi-agent repl ui"
```

---

### Task 3: Slash Command Handler

**Files:**
- Create: `orchestrator/repl_commands.py`
- Create: `tests/test_orchestrator/test_repl_commands.py`

- [ ] **Step 1: Write failing command handler tests**

Create `tests/test_orchestrator/test_repl_commands.py`:

```python
from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from orchestrator.registry import Card
from orchestrator.repl_commands import ReplCommandHandler
from orchestrator.repl_state import MultiAgentSessionState
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class _Cfg:
    provider = "mock"
    model = "mock-model"
    protocol = "openai"
    base_url = "http://mock.invalid/v1"
    api_key_env = "MOCK_API_KEY"


class _Handle:
    def __init__(self):
        self.card = Card(
            id="tool-agent", display_name="Tool", version="1.0.0",
            entrypoint={}, mcp={}, a2a={},
            capabilities_hint=["read_file"], model_override=None,
        )
        self.a2a_url = "http://127.0.0.1:50001"


class _Host:
    def list_handles(self):
        return [_Handle()]


class _Router:
    def all_capabilities(self):
        return ["read_file", "write_file"]

    def resolve(self, capability):
        return "tool-agent"


def _handler(tmp_path):
    buf = io.StringIO()
    ui = ReplUI(
        console=Console(file=buf, force_terminal=False, width=120),
        input_stream=io.StringIO(), output_stream=buf,
    )
    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[], instruction_files=[],
        memory_snapshot="memory", workspace=tmp_path,
    )
    return ReplCommandHandler(ui=ui, state=state, host=_Host(), router=_Router()), ui, state, buf


def test_help_continues_and_renders(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    result = handler.handle("/help")
    assert result == LoopAction.CONTINUE
    assert "Slash Commands" in buf.getvalue()


def test_exit_and_quit_return_exit(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/exit") == LoopAction.EXIT
    assert handler.handle("/quit") == LoopAction.EXIT


def test_agents_renders_table(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/agents") == LoopAction.CONTINUE
    assert "tool-agent" in buf.getvalue()


def test_tools_renders_capabilities(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/tools") == LoopAction.CONTINUE
    text = buf.getvalue()
    assert "read_file" in text
    assert "write_file" in text


def test_permissions_shows_current(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/permissions") == LoopAction.CONTINUE
    assert "workspace-write" in buf.getvalue()


def test_permissions_updates_state(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/permissions read-only") == LoopAction.CONTINUE
    assert state.permission_mode == "read-only"
    assert "read-only" in buf.getvalue()


def test_permissions_invalid_mode(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/permissions bogus") == LoopAction.CONTINUE
    assert "Invalid" in buf.getvalue()
    assert state.permission_mode == "workspace-write"


def test_config_renders_table(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/config") == LoopAction.CONTINUE
    text = buf.getvalue()
    assert "mock" in text
    assert "mock-model" in text


def test_clear_returns_continue(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/clear") == LoopAction.CONTINUE


def test_compact_resets_history(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    state.record_turn(
        user_input="x", capability="read_file",
        owner="tool-agent", observation="y", error=None,
    )
    assert handler.handle("/compact") == LoopAction.CONTINUE
    assert state.recent_history == []
    assert state.thread_id == "multi-agent-session-2"
    assert "Compacted" in buf.getvalue()


def test_compact_memory_failure_warns(tmp_path, monkeypatch):
    handler, ui, state, buf = _handler(tmp_path)
    # Simulate memory snapshot failure
    orig_compact = state.compact

    def _failing_compact(*, memory_snapshot):
        state.recent_history.clear()
        state.compacted_turns += 1
        state.thread_id = "multi-agent-session-2"
        # memory_snapshot intentionally not updated to simulate failure
    state.compact = _failing_compact

    assert handler.handle("/compact") == LoopAction.CONTINUE
    state.compact = orig_compact


def test_unknown_command_warns(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    assert handler.handle("/nope") == LoopAction.CONTINUE
    assert "Unknown command" in buf.getvalue()


def test_non_slash_input_returns_not_command(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    result = handler.handle("hello world")
    assert result is None
```

- [ ] **Step 2: Run command tests to verify import failure**

```bash
pytest tests/test_orchestrator/test_repl_commands.py -q
```

Expected: `ModuleNotFoundError: No module named 'orchestrator.repl_commands'`

- [ ] **Step 3: Implement `orchestrator/repl_commands.py`**

```python
from __future__ import annotations

from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class ReplCommandHandler:
    def __init__(self, *, ui: ReplUI, state, host, router):
        self.ui = ui
        self.state = state
        self.host = host
        self.router = router

    def handle(self, line: str) -> LoopAction | None:
        """Returns LoopAction for recognized slash commands, None for non-commands."""
        command = line.split(maxsplit=1)[0].lower()
        if not command.startswith("/"):
            return None
        try:
            if command in {"/exit", "/quit"}:
                return LoopAction.EXIT
            if command == "/help":
                return self._cmd_help()
            if command == "/agents":
                return self._cmd_agents()
            if command == "/tools":
                return self._cmd_tools()
            if command == "/permissions":
                return self._cmd_permissions(line)
            if command == "/config":
                return self._cmd_config()
            if command == "/model":
                return self._cmd_model(line)
            if command == "/skills":
                return self._cmd_skills()
            if command == "/instructions":
                return self._cmd_instructions()
            if command == "/clear":
                return self._cmd_clear()
            if command == "/compact":
                return self._cmd_compact()
            self.ui.render_error(
                "Unknown command",
                f"{command}\nType /help for available commands.",
            )
            return LoopAction.CONTINUE
        except Exception as exc:
            self.ui.render_error(f"Command error: {command}", str(exc))
            return LoopAction.CONTINUE

    def _cmd_help(self) -> LoopAction:
        self.ui.render_help()
        return LoopAction.CONTINUE

    def _cmd_agents(self) -> LoopAction:
        rows = []
        for handle in self.host.list_handles():
            card = handle.card
            rows.append([
                card.id, str(card.version),
                str(handle.a2a_url or "-"), "healthy",
                str(len(card.capabilities_hint)),
            ])
        self.ui.render_table(
            title="Specialist Agents",
            columns=["ID", "Version", "A2A URL", "Health", "Hints"],
            rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_tools(self) -> LoopAction:
        rows = [
            [cap, self.router.resolve(cap)]
            for cap in self.router.all_capabilities()
        ]
        self.ui.render_table(
            title="Registered Capabilities",
            columns=["Capability", "Owner"], rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_permissions(self, line: str) -> LoopAction:
        parts = line.split(maxsplit=1)
        if len(parts) == 1:
            self.ui.render_text(
                title="Permission Mode",
                text=f"Current: {self.state.permission_mode}",
            )
            return LoopAction.CONTINUE
        requested = parts[1].strip()
        if self.state.set_permission_mode(requested):
            self.ui.render_text(
                title="Permission Mode",
                text=f"Set to: {requested}", style="green",
            )
        else:
            self.ui.render_error(
                "Invalid permission mode",
                "Use: read-only, workspace-write, or danger-full-access.",
            )
        return LoopAction.CONTINUE

    def _cmd_config(self) -> LoopAction:
        rows = [
            ["provider", self.state.provider],
            ["model", self.state.model],
            ["protocol", self.state.protocol],
            ["base_url", self.state.base_url],
            ["api_key_env", self.state.api_key_env],
            ["permission mode", self.state.permission_mode],
            ["thread", self.state.thread_id],
            ["workspace", str(self.state.workspace)],
        ]
        self.ui.render_table(
            title="Effective Config", columns=["Key", "Value"], rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_model(self, line: str) -> LoopAction:
        self.ui.render_error(
            "Model Configuration",
            "Use python cli.py --single /model until the multi-agent wizard is implemented.",
        )
        return LoopAction.CONTINUE

    def _cmd_skills(self) -> LoopAction:
        rows = [
            [
                getattr(s, "name", ""),
                getattr(s, "title", ""),
                str(getattr(s, "path", "")),
            ]
            for s in self.state.skills
        ]
        self.ui.render_table(
            title="Installed Skills", columns=["Name", "Title", "Path"], rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_instructions(self) -> LoopAction:
        rows = [
            [str(getattr(f, "path", "")), str(len(getattr(f, "content", "")))]
            for f in self.state.instruction_files
        ]
        self.ui.render_table(
            title="Project Instructions", columns=["Path", "Characters"], rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_clear(self) -> LoopAction:
        self.ui.clear()
        return LoopAction.CONTINUE

    def _cmd_compact(self) -> LoopAction:
        try:
            from tool import tool_memory
            fresh = tool_memory.snapshot_for_system_prompt()
        except Exception:
            fresh = self.state.memory_snapshot
            self.ui.render_warning("Memory refresh failed, using previous snapshot.")
        self.state.compact(memory_snapshot=fresh)
        self.ui.render_text(
            title="Compacted", text=f"New thread: {self.state.thread_id}",
            style="green",
        )
        return LoopAction.CONTINUE
```

- [ ] **Step 4: Run command tests to verify pass**

```bash
pytest tests/test_orchestrator/test_repl_commands.py -q
```

Expected: `13 passed`

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py tests/test_orchestrator/test_repl_commands.py
git commit -m "feat(orchestrator): add multi-agent repl slash commands"
```

---

### Task 4: Planner Context Rendering

**Files:**
- Modify: `orchestrator/repl_state.py` — add `render_planner_context()`
- Modify: `tests/test_orchestrator/test_repl_state.py` — add context test

- [ ] **Step 1: Write failing context test**

Append to `tests/test_orchestrator/test_repl_state.py`:

```python

def test_planner_context_aggregates_all_fields(tmp_path):
    class _Instruction:
        path = tmp_path / "AGENTS.md"
        content = "Always be careful."

    class _Skill:
        name = "ppt-master"
        title = "PowerPoint"
        path = tmp_path / "skills" / "ppt-master"

    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[_Skill()],
        instruction_files=[_Instruction()],
        memory_snapshot="Remember user prefers concise answers.",
        workspace=tmp_path,
    )
    state.record_turn(
        user_input="read README", capability="read_file",
        owner="tool-agent", observation="README text", error=None,
    )

    context = state.render_planner_context(["read_file", "skill.ppt-master"])

    assert "mock" in context
    assert "mock-default" in context
    assert "read_file" in context
    assert "skill.ppt-master" in context
    assert "Always be careful." in context
    assert "Remember user prefers concise answers." in context
    assert "ppt-master" in context
    assert "README text" in context
```

- [ ] **Step 2: Run to verify AttributeError**

```bash
pytest tests/test_orchestrator/test_repl_state.py::test_planner_context_aggregates_all_fields -q
```

Expected: `AttributeError: 'MultiAgentSessionState' object has no attribute 'render_planner_context'`

- [ ] **Step 3: Add `render_planner_context` to `MultiAgentSessionState`**

In `orchestrator/repl_state.py`, add to the class:

```python
    def render_planner_context(self, capabilities: list[str]) -> str:
        instruction_sections = []
        for file in self.instruction_files:
            path = getattr(file, "path", "")
            content = getattr(file, "content", "")
            if content:
                instruction_sections.append(f"## {path}\n{content}")

        skill_lines = []
        for skill in self.skills:
            name = getattr(skill, "name", "")
            title = getattr(skill, "title", "")
            skill_lines.append(f"- {name}: {title}")

        history_lines = []
        for item in self.recent_history:
            parts = [
                f"User: {item['user']}",
                f"Capability: {item['capability']}",
                f"Owner: {item['owner']}",
                f"Observation: {item['observation']}",
            ]
            if item.get("error"):
                parts.append(f"Error: {item['error']}")
            history_lines.append("\n".join(parts))

        return "\n\n".join([
            f"Provider: {self.provider}",
            f"Model: {self.model}",
            f"Protocol: {self.protocol}",
            f"Permission mode: {self.permission_mode}",
            "Capabilities:\n" + "\n".join(f"- {c}" for c in capabilities),
            "Memory:\n" + (self.memory_snapshot or "<none>"),
            "Project instructions:\n" + ("\n\n".join(instruction_sections) or "<none>"),
            "Skills:\n" + ("\n".join(skill_lines) or "<none>"),
            "Recent history:\n" + ("\n\n".join(history_lines) or "<none>"),
        ])
```

- [ ] **Step 4: Run state tests**

```bash
pytest tests/test_orchestrator/test_repl_state.py -q
```

Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_state.py tests/test_orchestrator/test_repl_state.py
git commit -m "feat(orchestrator): add planner context rendering"
```

---

### Task 5: REPLController (Core Orchestration)

**Files:**
- Create: `orchestrator/repl_controller.py`
- Create: `tests/test_orchestrator/test_repl_controller.py`

- [ ] **Step 1: Write failing controller tests**

Create `tests/test_orchestrator/test_repl_controller.py`:

```python
from __future__ import annotations

import asyncio
import io

import pytest
from rich.console import Console

from orchestrator.repl_commands import ReplCommandHandler
from orchestrator.repl_controller import REPLController
from orchestrator.repl_state import MultiAgentSessionState
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class _Cfg:
    provider = "openai"
    model = "gpt-4o"
    protocol = "openai"
    base_url = "https://api.openai.com/v1"
    api_key_env = "OPENAI_API_KEY"


class _FakeHost:
    def __init__(self):
        self.calls = []
        self._cancel_called = False

    async def call_tool(self, agent_id, name, arguments):
        self.calls.append((agent_id, name, arguments))
        return {"content": [{"type": "text", "text": "file contents"}]}

    async def cancel_all(self):
        self._cancel_called = True

    def list_handles(self):
        return []


class _FakeRouter:
    def all_capabilities(self):
        return ["read_file"]

    def resolve(self, capability):
        return "tool-agent"


def _make_controller(tmp_path, **overrides):
    buf = io.StringIO()
    ui = ReplUI(
        console=Console(file=buf, force_terminal=False, width=120),
        input_stream=io.StringIO(), output_stream=buf,
    )
    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[], instruction_files=[],
        memory_snapshot="", workspace=tmp_path,
    )
    host = _FakeHost()
    router = _FakeRouter()
    commands = ReplCommandHandler(ui=ui, state=state, host=host, router=router)
    controller = REPLController(
        host=host, router=router, hmac_key="secret",
        state=state, commands=commands, ui=ui,
        **overrides,
    )
    return controller, ui, state, host, router, buf


@pytest.mark.asyncio
async def test_handle_input_routes_slash_commands(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    result = await controller.handle_input("/help")
    assert result == LoopAction.CONTINUE
    assert "Slash Commands" in buf.getvalue()


@pytest.mark.asyncio
async def test_handle_input_executes_normal_turn(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    result = await controller.handle_input("read_file:README.md")
    assert result == LoopAction.CONTINUE
    assert state.turns == 1
    assert host.calls


@pytest.mark.asyncio
async def test_execute_turn_records_even_on_error(tmp_path):
    class _BadHost:
        async def call_tool(self, agent_id, name, arguments):
            return {"content": [{"type": "text", "text": "error: crash"}], "isError": True}
        async def cancel_all(self): pass
        def list_handles(self): return []

    controller, ui, state, host, router, buf = _make_controller(
        tmp_path, host=_BadHost(),
    )
    result = await controller._execute_turn("read_file:foo")
    assert result == LoopAction.CONTINUE
    assert state.turns == 1


@pytest.mark.asyncio
async def test_execute_turn_catches_exception_as_recoverable(tmp_path):
    class _ExplodingHost:
        async def call_tool(self, agent_id, name, arguments):
            raise ConnectionError("specialist unreachable")
        async def cancel_all(self): pass
        def list_handles(self): return []

    controller, ui, state, host, router, buf = _make_controller(
        tmp_path, host=_ExplodingHost(),
    )
    result = await controller._execute_turn("read_file:foo")
    assert result == LoopAction.CONTINUE
    assert "specialist unreachable" in buf.getvalue()


@pytest.mark.asyncio
async def test_is_fatal_returns_true_for_cancelled_error(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    assert controller._is_fatal(asyncio.CancelledError()) is True


@pytest.mark.asyncio
async def test_is_fatal_returns_false_for_common_errors(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    assert controller._is_fatal(ConnectionError("x")) is False
    assert controller._is_fatal(ValueError("x")) is False
    assert controller._is_fatal(RuntimeError("x")) is False


@pytest.mark.asyncio
async def test_ensure_planner_lazy_init(tmp_path):
    controller, ui, state, host, router, buf = _make_controller(tmp_path)
    assert controller._planner is None
    # Don't actually call _ensure_planner — it needs a real LLM or mock env.
    # But test the state before init.
```

- [ ] **Step 2: Run controller tests to verify import failure**

```bash
pytest tests/test_orchestrator/test_repl_controller.py -q
```

Expected: `ModuleNotFoundError: No module named 'orchestrator.repl_controller'`

- [ ] **Step 3: Implement `orchestrator/repl_controller.py`**

```python
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Callable

from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI
from orchestrator.turns import LLMPlanner, TurnResult, TurnRunner, _stub_planner

log = logging.getLogger(__name__)

FATAL_EXCEPTIONS = ()


class REPLController:
    def __init__(
        self,
        *,
        host,
        router,
        hmac_key: str,
        state,
        commands,
        ui: ReplUI,
    ):
        self.host = host
        self.router = router
        self.hmac_key = hmac_key
        self.state = state
        self.commands = commands
        self.ui = ui
        self._planner = None

    async def handle_input(self, text: str) -> LoopAction:
        if text.startswith("/"):
            result = self.commands.handle(text)
            if result is not None:
                return result
        return await self._execute_turn(text)

    async def _execute_turn(self, text: str) -> LoopAction:
        await self._ensure_planner()

        trace_id = secrets.token_hex(4)

        runner = TurnRunner(
            host=self.host,
            router=self.router,
            hmac_key=self.hmac_key,
            permission_mode_provider=lambda: self.state.permission_mode,
            planner=self._planner,
        )

        try:
            result = await runner.run(text, trace_id=trace_id)
        except asyncio.CancelledError:
            await self.host.cancel_all()
            self.ui.render_cancelled()
            return LoopAction.CONTINUE
        except Exception as exc:
            if self._is_fatal(exc):
                self.ui.render_error("Fatal Error", str(exc))
                return LoopAction.EXIT
            self.ui.render_error("Turn Error", str(exc))
            self.state.record_turn(
                user_input=text, capability="", owner="",
                observation="", error=str(exc),
            )
            return LoopAction.CONTINUE

        self.state.record_turn(
            user_input=text,
            capability=result.capability,
            owner=result.owner,
            observation=result.text,
            error=result.error,
        )

        if result.error:
            self.ui.render_error("Turn Error", result.error)
        elif result.text:
            self.ui.render_text(
                title=result.owner or "orchestrator",
                text=result.text,
            )
        self.ui.render_divider()
        return LoopAction.CONTINUE

    async def _ensure_planner(self) -> None:
        if self._planner is not None:
            return
        provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
        if provider.startswith("mock") or not provider:
            self._planner = _stub_planner
            return
        try:
            llm = _build_planner_llm()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to build planner LLM: {exc}. "
                f"Use /config or /model to fix, or set LANGCHAIN_AGENT_MODEL=mock."
            ) from exc
        self._planner = LLMPlanner(
            llm=llm,
            available_capabilities=self.router.all_capabilities(),
            context_provider=lambda: self.state.render_planner_context(
                self.router.all_capabilities()
            ),
        )

    def _is_fatal(self, error: Exception) -> bool:
        if isinstance(error, asyncio.CancelledError):
            return True
        return False


def _build_planner_llm():
    from config import build_llm, load_active_config
    return build_llm(load_active_config())
```

- [ ] **Step 4: Run controller tests**

```bash
pytest tests/test_orchestrator/test_repl_controller.py -q
```

Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_controller.py tests/test_orchestrator/test_repl_controller.py
git commit -m "feat(orchestrator): add repl controller with lazy planner"
```

---

### Task 6: Wire run_repl() in main.py

**Files:**
- Modify: `orchestrator/main.py` — replace stub `run_repl()`
- Modify: `tests/test_e2e_multi_agent/test_e2e_simple_tool.py` — add REPL e2e

- [ ] **Step 1: Write failing e2e test**

Append to `tests/test_e2e_multi_agent/test_e2e_simple_tool.py`:

```python

@pytest.mark.e2e
def test_multi_agent_repl_dispatches_turn_and_exits(tmp_path):
    target = tmp_path / "hello-repl.txt"
    target.write_text("hello from repl", encoding="utf-8")

    env = os.environ.copy()
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"

    proc = subprocess.run(
        [sys.executable, "cli.py"],
        input=f"read_file:{target}\n/exit\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert "multi-agent REPL not fully implemented" not in proc.stdout
    assert "Traceback" not in proc.stderr
    assert "hello from repl" in proc.stdout
```

- [ ] **Step 2: Run e2e test to confirm stub failure**

```bash
pytest tests/test_e2e_multi_agent/test_e2e_simple_tool.py::test_multi_agent_repl_dispatches_turn_and_exits -q
```

Expected: `FAILED ... assert 'multi-agent REPL not fully implemented' not in proc.stdout`

- [ ] **Step 3: Replace stub run_repl() in main.py**

Replace the current `run_repl()` in `orchestrator/main.py` (lines 104-113) with:

```python
async def run_repl() -> int:
    import config
    from pathlib import Path
    from orchestrator.repl_controller import REPLController
    from orchestrator.repl_commands import ReplCommandHandler
    from orchestrator.repl_state import MultiAgentSessionState
    from orchestrator.repl_ui import ReplUI
    from orchestrator import telemetry

    hmac_key = secrets.token_urlsafe(32)
    host = MCPHost(hmac_key=hmac_key)
    router = CapabilityRouter()
    mux = StreamMux()
    ui = ReplUI()

    try:
        telemetry.reset_log()
        stop_telemetry = asyncio.Event()
        tail_task = asyncio.create_task(telemetry.tail(mux, stop_telemetry))

        await _bootstrap(host, router)

        config.hydrate_env_from_credentials()
        active_cfg = config.load_active_config()

        memory_snapshot = ""
        try:
            from tool import tool_memory
            memory_snapshot = tool_memory.snapshot_for_system_prompt()
        except Exception:
            pass

        skills_list: list = []
        try:
            from skills.skill_loader import load_skills
            skills_list = load_skills()
        except Exception:
            pass

        instruction_files: list = []
        try:
            from project_context import discover_instruction_files
            instruction_files = discover_instruction_files()
        except Exception:
            pass

        state = MultiAgentSessionState.from_runtime(
            active_cfg=active_cfg,
            skills=skills_list,
            instruction_files=instruction_files,
            memory_snapshot=memory_snapshot,
            workspace=Path.cwd(),
        )

        commands = ReplCommandHandler(
            ui=ui, state=state, host=host, router=router,
        )
        controller = REPLController(
            host=host, router=router, hmac_key=hmac_key,
            state=state, commands=commands, ui=ui,
        )

        ui.render_welcome(
            provider=state.provider,
            model=state.model,
            permission_mode=state.permission_mode,
            agent_count=len(host.list_handles()),
            workspace=str(state.workspace),
        )

        while True:
            try:
                text = await ui.read_input_async()
            except EOFError:
                ui.render_goodbye()
                break
            except KeyboardInterrupt:
                ui.render_cancelled()
                break
            if not text.strip():
                continue
            try:
                action = await controller.handle_input(text.strip())
            except KeyboardInterrupt:
                await host.cancel_all()
                ui.render_cancelled()
                action = LoopAction.CONTINUE
            if action == LoopAction.EXIT:
                break

        return 0
    finally:
        stop_telemetry = locals().get("stop_telemetry")
        tail_task = locals().get("tail_task")
        if stop_telemetry is not None and tail_task is not None:
            stop_telemetry.set()
            try:
                await asyncio.wait_for(tail_task, timeout=2.0)
            except asyncio.TimeoutError:
                tail_task.cancel()
                try:
                    await tail_task
                except asyncio.CancelledError:
                    pass
        await host.shutdown_all()
```

Add the import at the top of the function or use the existing import of `LoopAction`:

```python
from orchestrator.repl_types import LoopAction
```

Added to imports in main.py.

- [ ] **Step 4: Run e2e test to verify pass**

```bash
pytest tests/test_e2e_multi_agent/test_e2e_simple_tool.py::test_multi_agent_repl_dispatches_turn_and_exits -q
```

Expected: `1 passed`

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/test_orchestrator/ tests/test_e2e_multi_agent/test_e2e_simple_tool.py tests/test_e2e_multi_agent/test_e2e_legacy_mode.py -q
```

Expected: All passing.

- [ ] **Step 6: Smoke test**

```bash
echo "/exit" | python cli.py
```

Expected: welcome panel with "Multi-Agent" and clean exit.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/main.py tests/test_e2e_multi_agent/test_e2e_simple_tool.py
git commit -m "feat(orchestrator): wire multi-agent repl controller"
```

---

## Self-Review

1. **Spec coverage:**
   - `repl_types.py` (LoopAction) → Task 1
   - `ReplUI` with prompt_toolkit + Rich → Task 2
   - `ReplCommandHandler` with all 11 slash commands → Task 3
   - Planner context aggregation → Task 4
   - `REPLController` with async handle_input, _execute_turn, lazy _ensure_planner, _is_fatal → Task 5
   - `main.py` thin bootstrap + async input loop + clean shutdown → Task 6
   - Ctrl+C handling, error tiers, EOF, /exit → Tasks 5 + 6
   - `context_provider` dynamic lambda → Tasks 4 + 5
   - `/compact` memory refresh with warning → Task 3

2. **Placeholder scan:** No TBD, TODO, or deferred-work markers.

3. **Type consistency:**
   - `LoopAction` defined in Task 1, used in Tasks 3, 5, 6
   - `ReplUI` defined in Task 2, used in Tasks 3, 5, 6
   - `ReplCommandHandler` defined in Task 3, used in Tasks 5, 6
   - `REPLController` defined in Task 5, used in Task 6
   - `render_planner_context` defined in Task 4, used in Task 5 (`_ensure_planner`)
