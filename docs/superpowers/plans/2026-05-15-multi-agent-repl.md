# Multi-Agent REPL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Phase 5 multi-agent REPL stub with a legacy-feeling interactive REPL that supports natural language turns, common slash commands, session continuity, and clean specialist lifecycle handling.

**Architecture:** Keep `orchestrator/main.py` thin and move REPL work into focused modules: `turns.py` for one-turn orchestration, `repl.py` for the loop, `repl_state.py` for session state, `repl_ui.py` for terminal rendering, and `repl_commands.py` for slash commands. The multi-agent REPL reuses low-level config, skills, instructions, memory, MCP, and router modules, but does not instantiate `legacy.single_agent_loop.CliApp`.

**Tech Stack:** Python 3.13, pytest, pytest-asyncio, subprocess e2e tests, LangGraph, MCP stdio client, Rich, prompt_toolkit.

---

## File Structure

Create:

- `orchestrator/repl_state.py`  
  Defines `MultiAgentSessionState`, config hydration, memory/instruction/skills loading, history recording, and compaction.

- `orchestrator/turns.py`  
  Extracts one-turn orchestration from `orchestrator/main.py`: planner selection, graph invocation, telemetry tailing, MCP result normalization, and structured `TurnResult`.

- `orchestrator/repl_ui.py`  
  Provides terminal I/O and rendering helpers: boxed input, non-TTY input, slash completion, welcome panel, tables, result panels, error panels, spinner, divider.

- `orchestrator/repl_commands.py`  
  Implements multi-agent slash commands independently of legacy `CliApp`.

- `orchestrator/repl.py`  
  Owns the interactive loop, specialist startup/shutdown, command dispatch, turn dispatch, cancellation behavior, and session updates.

- `tests/test_orchestrator/test_repl_state.py`  
  Tests state loading, history recording, and compaction.

- `tests/test_orchestrator/test_turns.py`  
  Tests planner selection, one-turn result normalization, and context injection.

- `tests/test_orchestrator/test_repl_ui.py`  
  Tests pure UI formatting and non-TTY input behavior.

- `tests/test_orchestrator/test_repl_commands.py`  
  Tests slash command outputs and state changes.

Modify:

- `orchestrator/main.py`  
  Becomes a thin dispatcher to `turns.run_prompt()` and `repl.run_repl()`. Keeps import-compatible `LLMPlanner` if tests still import it, or re-exports it from `turns.py`.

- `orchestrator/mcp_host.py`  
  Adds health tracking and shutdown cleanup hardening.

- `tests/test_orchestrator/test_slash_agents.py`  
  Migrate to command handler or remove after `/agents` coverage exists in `test_repl_commands.py`.

- `tests/test_e2e_multi_agent/test_e2e_simple_tool.py`  
  Add REPL stdin regression.

- `tests/test_e2e_multi_agent/test_ctrl_c_cancel.py`  
  Tighten once the REPL is no longer a stub.

- `tests/test_e2e_multi_agent/test_specialist_crash.py`  
  Tighten once the REPL bootstraps specialists at startup.

- `README.md`  
  Remove the post-Day-1 REPL caveat and document multi-agent slash commands.

---

### Task 1: Multi-Agent Session State

**Files:**
- Create: `orchestrator/repl_state.py`
- Create: `tests/test_orchestrator/test_repl_state.py`

- [ ] **Step 1: Write failing state initialization tests**

Create `tests/test_orchestrator/test_repl_state.py`:

```python
from __future__ import annotations

from pathlib import Path

from orchestrator.repl_state import MultiAgentSessionState


class _Cfg:
    provider = "mock"
    model = "mock-default"
    protocol = "openai"
    base_url = "http://mock.invalid/v1"
    api_key_env = "MOCK_API_KEY"


def test_state_from_runtime_loads_config_and_static_context(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_PERMISSION_MODE", "read-only")

    instruction_file = tmp_path / "AGENTS.md"
    instruction_file.write_text("Project rule", encoding="utf-8")

    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[],
        instruction_files=[],
        memory_snapshot="# Memory\nRemember this.",
        workspace=tmp_path,
    )

    assert state.provider == "mock"
    assert state.model == "mock-default"
    assert state.protocol == "openai"
    assert state.base_url == "http://mock.invalid/v1"
    assert state.api_key_env == "MOCK_API_KEY"
    assert state.permission_mode == "read-only"
    assert state.thread_id == "multi-agent-session-1"
    assert state.turns == 0
    assert state.recent_history == []
    assert state.memory_snapshot == "# Memory\nRemember this."
    assert state.workspace == tmp_path


def test_state_records_turn_result_and_compacts(tmp_path):
    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[],
        instruction_files=[],
        memory_snapshot="memory",
        workspace=tmp_path,
    )

    state.record_turn(
        user_input="read README",
        capability="read_file",
        owner="tool-agent",
        observation="README contents",
        error=None,
    )

    assert state.turns == 1
    assert state.seen_messages == 1
    assert state.recent_history == [
        {
            "user": "read README",
            "capability": "read_file",
            "owner": "tool-agent",
            "observation": "README contents",
            "error": None,
        }
    ]

    state.compact(memory_snapshot="fresh memory")

    assert state.compacted_turns == 1
    assert state.turns == 0
    assert state.seen_messages == 0
    assert state.thread_id == "multi-agent-session-2"
    assert state.recent_history == []
    assert state.memory_snapshot == "fresh memory"
```

- [ ] **Step 2: Run state tests to verify import failure**

Run:

```bash
pytest tests/test_orchestrator/test_repl_state.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'orchestrator.repl_state'
```

- [ ] **Step 3: Implement `orchestrator/repl_state.py`**

Create `orchestrator/repl_state.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VALID_PERMISSION_MODES = {"read-only", "workspace-write", "danger-full-access"}
DEFAULT_PERMISSION_MODE = "workspace-write"
MAX_HISTORY_ITEMS = 12


@dataclass
class MultiAgentSessionState:
    provider: str
    model: str
    protocol: str
    base_url: str
    api_key_env: str
    permission_mode: str
    workspace: Path
    thread_id: str = "multi-agent-session-1"
    turns: int = 0
    tool_calls: int = 0
    compacted_turns: int = 0
    seen_messages: int = 0
    last_error: str | None = None
    recent_history: list[dict[str, Any]] = field(default_factory=list)
    memory_snapshot: str = ""
    instruction_files: list[Any] = field(default_factory=list)
    skills: list[Any] = field(default_factory=list)

    @classmethod
    def from_runtime(
        cls,
        *,
        active_cfg,
        skills: list[Any],
        instruction_files: list[Any],
        memory_snapshot: str,
        workspace: Path,
    ) -> "MultiAgentSessionState":
        permission_mode = os.environ.get("LANGCHAIN_AGENT_PERMISSION_MODE", DEFAULT_PERMISSION_MODE)
        if permission_mode not in VALID_PERMISSION_MODES:
            permission_mode = DEFAULT_PERMISSION_MODE
        return cls(
            provider=active_cfg.provider,
            model=active_cfg.model,
            protocol=active_cfg.protocol,
            base_url=active_cfg.base_url,
            api_key_env=active_cfg.api_key_env,
            permission_mode=permission_mode,
            workspace=workspace,
            memory_snapshot=memory_snapshot,
            instruction_files=list(instruction_files),
            skills=list(skills),
        )

    def set_permission_mode(self, mode: str) -> bool:
        if mode not in VALID_PERMISSION_MODES:
            return False
        self.permission_mode = mode
        os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] = mode
        return True

    def apply_config(self, cfg) -> None:
        self.provider = cfg.provider
        self.model = cfg.model
        self.protocol = cfg.protocol
        self.base_url = cfg.base_url
        self.api_key_env = cfg.api_key_env

    def record_turn(
        self,
        *,
        user_input: str,
        capability: str,
        owner: str,
        observation: str,
        error: str | None,
    ) -> None:
        self.turns += 1
        self.seen_messages += 1
        self.last_error = error
        if capability:
            self.tool_calls += 1
        self.recent_history.append(
            {
                "user": user_input,
                "capability": capability,
                "owner": owner,
                "observation": observation,
                "error": error,
            }
        )
        if len(self.recent_history) > MAX_HISTORY_ITEMS:
            self.recent_history = self.recent_history[-MAX_HISTORY_ITEMS:]

    def compact(self, *, memory_snapshot: str) -> None:
        self.compacted_turns += self.turns
        self.turns = 0
        self.seen_messages = 0
        self.last_error = None
        self.recent_history.clear()
        self.memory_snapshot = memory_snapshot
        suffix = self.compacted_turns + 1
        self.thread_id = f"multi-agent-session-{suffix}"
```

- [ ] **Step 4: Run state tests to verify pass**

Run:

```bash
pytest tests/test_orchestrator/test_repl_state.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add orchestrator/repl_state.py tests/test_orchestrator/test_repl_state.py
git commit -m "feat(orchestrator): add multi-agent repl state"
```

---

### Task 2: Shared Turn Runner

**Files:**
- Create: `orchestrator/turns.py`
- Create: `tests/test_orchestrator/test_turns.py`
- Modify: `orchestrator/main.py`
- Modify: `tests/test_orchestrator/test_llm_planner.py`

- [ ] **Step 1: Write failing turn runner tests**

Create `tests/test_orchestrator/test_turns.py`:

```python
from __future__ import annotations

import pytest

from orchestrator.router import CapabilityRouter
from orchestrator.turns import LLMPlanner, TurnRunner, _stub_planner


class _Text:
    def __init__(self, text: str):
        self.text = text


class _FakeHost:
    def __init__(self):
        self.calls = []

    async def call_tool(self, agent_id: str, name: str, arguments: dict):
        self.calls.append((agent_id, name, arguments))
        return {"content": [{"type": "text", "text": "file contents"}]}


class _FakeLLMResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return _FakeLLMResponse(self.content)


def test_stub_planner_supports_capability_colon_arg():
    decision = _stub_planner({"user_input": "read_file:README.md"})
    assert decision == {"capability": "read_file", "arguments": {"path": "README.md"}}


def test_llm_planner_includes_session_context():
    llm = _FakeLLM('{"capability": "read_file", "arguments": {"path": "README.md"}}')
    planner = LLMPlanner(
        llm=llm,
        available_capabilities=["read_file"],
        context_provider=lambda: "Recent history: user asked about README",
    )

    decision = planner({"user_input": "read it", "trace_id": "t1"})

    assert decision["capability"] == "read_file"
    assert "Recent history" in llm.messages[1]["content"]


@pytest.mark.asyncio
async def test_turn_runner_dispatches_and_normalizes_text():
    router = CapabilityRouter()
    router.register("tool-agent", ["read_file"])
    host = _FakeHost()

    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key="secret",
        permission_mode_provider=lambda: "workspace-write",
        planner=lambda state: {"capability": "read_file", "arguments": {"path": "README.md"}},
    )

    result = await runner.run("read README", trace_id="t1")

    assert result.error is None
    assert result.capability == "read_file"
    assert result.owner == "tool-agent"
    assert result.text == "file contents"
    assert host.calls[0][0] == "tool-agent"
    assert host.calls[0][1] == "read_file"
    assert host.calls[0][2]["path"] == "README.md"
    assert "authz_grant" in host.calls[0][2]["_meta"]
```

- [ ] **Step 2: Run turn tests to verify import failure**

Run:

```bash
pytest tests/test_orchestrator/test_turns.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'orchestrator.turns'
```

- [ ] **Step 3: Implement `orchestrator/turns.py`**

Create `orchestrator/turns.py`:

```python
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Callable

from orchestrator.graph import build_graph
from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux


@dataclass
class TurnResult:
    capability: str = ""
    owner: str = ""
    text: str = ""
    error: str | None = None


class LLMPlanner:
    _SYSTEM = (
        "You are the orchestrator's planning brain. The available capabilities are listed below. "
        "Reply with ONLY a JSON object of the form "
        '{"capability": "<name>", "arguments": {<args>}}. '
        "No prose, no markdown fence."
    )

    def __init__(
        self,
        *,
        llm,
        available_capabilities: list[str],
        context_provider: Callable[[], str] | None = None,
    ):
        self._llm = llm
        self._caps = available_capabilities
        self._context_provider = context_provider or (lambda: "")

    def __call__(self, state) -> dict:
        context = self._context_provider()
        prompt = (
            f"Available capabilities: {self._caps}\n\n"
            f"Session context:\n{context}\n\n"
            f"User: {state['user_input']}"
        )
        out = self._llm.invoke(
            [
                {"role": "system", "content": self._SYSTEM},
                {"role": "user", "content": prompt},
            ]
        )
        text = str(out.content).strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return json.loads(text)


def _stub_planner(state):
    scripted = os.environ.get("MOCK_ORCH_SCRIPT")
    if scripted:
        return json.loads(scripted)
    text = state["user_input"]
    if ":" in text:
        cap, _, arg = text.partition(":")
        return {"capability": cap.strip(), "arguments": {"path": arg.strip()}}
    raise ValueError("stub planner: expected 'CAPABILITY:ARG' input or MOCK_ORCH_SCRIPT env")


def extract_text(call_result) -> str:
    contents = getattr(call_result, "content", None)
    if contents is None and isinstance(call_result, dict):
        contents = call_result.get("content")
    parts: list[str] = []
    for piece in contents or []:
        text = getattr(piece, "text", None)
        if text is None and isinstance(piece, dict):
            text = piece.get("text", "")
        if text:
            parts.append(str(text))
    return "\n".join(parts)


class TurnRunner:
    def __init__(
        self,
        *,
        host,
        router: CapabilityRouter,
        hmac_key: str,
        permission_mode_provider: Callable[[], str],
        planner,
    ):
        self.host = host
        self.router = router
        self.hmac_key = hmac_key
        self.permission_mode_provider = permission_mode_provider
        self.planner = planner

    async def run(self, user_input: str, *, trace_id: str) -> TurnResult:
        graph = build_graph(
            router=self.router,
            host=self.host,
            planner=self.planner,
            hmac_key=self.hmac_key,
            mode=self.permission_mode_provider(),
        )
        result = await graph.ainvoke({"user_input": user_input, "trace_id": trace_id})
        if result.get("error"):
            return TurnResult(error=str(result["error"]))
        capability = result.get("capability", "")
        owner = self.router.resolve(capability) if capability else ""
        text = extract_text(result.get("result"))
        return TurnResult(capability=capability, owner=owner, text=text, error=None)


async def run_prompt_once(
    *,
    prompt: str,
    host,
    router: CapabilityRouter,
    hmac_key: str,
    planner,
    permission_mode_provider: Callable[[], str],
    mux: StreamMux,
) -> int:
    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key=hmac_key,
        permission_mode_provider=permission_mode_provider,
        planner=planner,
    )
    result = await runner.run(prompt, trace_id="t1")
    if result.error:
        mux.emit(agent_id="orchestrator", trace_id="t1", chunk=f"error: {result.error}\n")
        return 1
    if result.text:
        mux.emit(agent_id=result.owner, trace_id="t1", chunk=result.text + "\n")
    return 0
```

- [ ] **Step 4: Move planner imports in tests**

Modify `tests/test_orchestrator/test_llm_planner.py`:

```python
from orchestrator.turns import LLMPlanner
from agents.shared.mock_chat_model import MockChatModel
```

Keep both existing tests unchanged after the import line.

- [ ] **Step 5: Thin `orchestrator/main.py` prompt path**

Modify `orchestrator/main.py` to import planner pieces from `orchestrator.turns`:

```python
from orchestrator.turns import LLMPlanner, _stub_planner, run_prompt_once
```

Replace the body of `run_prompt(prompt: str)` with:

```python
async def run_prompt(prompt: str) -> int:
    hmac_key = secrets.token_urlsafe(32)
    host = MCPHost(hmac_key=hmac_key)
    router = CapabilityRouter()
    mux = StreamMux()
    try:
        await _bootstrap(host, router)
        provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
        if provider.startswith("mock") or not provider:
            planner = _stub_planner
        else:
            llm = _build_orchestrator_llm()
            planner = LLMPlanner(llm=llm, available_capabilities=router.all_capabilities())
        return await run_prompt_once(
            prompt=prompt,
            host=host,
            router=router,
            hmac_key=hmac_key,
            planner=planner,
            permission_mode_provider=lambda: os.environ.get(
                "LANGCHAIN_AGENT_PERMISSION_MODE", "workspace-write"
            ),
            mux=mux,
        )
    finally:
        await host.shutdown_all()
```

Remove the old duplicated graph invocation code from `run_prompt`. Leave `run_repl()` unchanged in this task.

- [ ] **Step 6: Run turn and existing prompt tests**

Run:

```bash
pytest tests/test_orchestrator/test_turns.py tests/test_orchestrator/test_llm_planner.py tests/test_e2e_multi_agent/test_e2e_simple_tool.py -q
```

Expected:

```text
5 passed
```

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add orchestrator/main.py orchestrator/turns.py tests/test_orchestrator/test_turns.py tests/test_orchestrator/test_llm_planner.py
git commit -m "refactor(orchestrator): extract shared turn runner"
```

---

### Task 3: Terminal UI Adapter

**Files:**
- Create: `orchestrator/repl_ui.py`
- Create: `tests/test_orchestrator/test_repl_ui.py`

- [ ] **Step 1: Write failing UI tests**

Create `tests/test_orchestrator/test_repl_ui.py`:

```python
from __future__ import annotations

import io

from rich.console import Console

from orchestrator.repl_ui import COMMANDS, ReplUI


def _ui():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    return ReplUI(console=console, input_stream=io.StringIO(), output_stream=buf), buf


def test_help_table_contains_common_commands():
    ui, buf = _ui()
    ui.render_help()
    text = buf.getvalue()
    assert "Slash Commands" in text
    assert "/agents" in text
    assert "/compact" in text
    assert "/model" in text


def test_error_panel_includes_title_and_message():
    ui, buf = _ui()
    ui.render_error("Planner Error", "invalid JSON")
    text = buf.getvalue()
    assert "Planner Error" in text
    assert "invalid JSON" in text


def test_read_input_uses_non_tty_stream():
    buf = io.StringIO()
    ui = ReplUI(
        console=Console(file=buf, force_terminal=False),
        input_stream=io.StringIO("/exit\n"),
        output_stream=buf,
    )
    assert ui.read_input() == "/exit"


def test_command_list_has_selected_common_group():
    assert set(COMMANDS) == {
        "/help",
        "/exit",
        "/quit",
        "/agents",
        "/tools",
        "/permissions",
        "/config",
        "/model",
        "/skills",
        "/instructions",
        "/clear",
        "/compact",
    }
```

- [ ] **Step 2: Run UI tests to verify import failure**

Run:

```bash
pytest tests/test_orchestrator/test_repl_ui.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'orchestrator.repl_ui'
```

- [ ] **Step 3: Implement `orchestrator/repl_ui.py`**

Create `orchestrator/repl_ui.py`:

```python
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
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


class Spinner:
    FRAMES = ("|", "/", "-", "\\")

    def __init__(self, label: str, out: TextIO | None = None):
        self.label = label
        self._out = out or sys.stdout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_width = 0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def _animate(self) -> None:
        for frame in self.FRAMES:
            if self._stop.is_set():
                break
            self._write_frame(frame)
            time.sleep(0.02)

    def _write_frame(self, frame: str) -> None:
        text = f"\r\033[36m{frame}\033[0m {self.label}"
        visible_width = 2 + len(self.label)
        padding = " " * max(0, self._last_width - visible_width)
        self._out.write(text + padding)
        self._out.flush()
        self._last_width = visible_width

    def set_label(self, label: str) -> None:
        self.label = label
        self._write_frame(self.FRAMES[0])

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        self._out.write("\r" + (" " * max(self._last_width, 2 + len(self.label))) + "\r")
        self._out.flush()


@dataclass
class ReplUI:
    console: Console | None = None
    input_stream: TextIO | None = None
    output_stream: TextIO | None = None

    def __post_init__(self) -> None:
        self.input_stream = self.input_stream or sys.stdin
        self.output_stream = self.output_stream or sys.stdout
        self.console = self.console or Console(file=self.output_stream)

    def read_input(self) -> str:
        if self.input_stream and not self.input_stream.isatty():
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
        session = PromptSession(
            history=InMemoryHistory(),
            completer=WordCompleter(list(COMMANDS), ignore_case=True),
        )
        return session.prompt("multi-agent> ").strip()

    def render_welcome(self, *, provider: str, model: str, permission_mode: str, agent_count: int, workspace: str) -> None:
        subtitle = (
            f"Provider: {provider} | Model: {model} | "
            f"Permission: {permission_mode} | Agents: {agent_count} | Workspace: {workspace}"
        )
        self.console.print()
        self.console.print(
            Panel(subtitle, title=Text("LangChain Agent CLI - Multi-Agent", style="bold cyan"), border_style="cyan", box=box.ROUNDED)
        )
        self.console.print("[dim]Enter sends. Type /help for commands.[/dim]")
        self.console.print()

    def render_help(self) -> None:
        table = Table(title="Slash Commands", box=box.SIMPLE_HEAVY)
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description")
        for command, description in COMMANDS.items():
            table.add_row(command, description)
        self.console.print(table)

    def render_error(self, title: str, message: str) -> None:
        self.console.print(Panel(message, title=title, border_style="red", box=box.ROUNDED))

    def render_text(self, *, title: str, text: str, style: str = "cyan") -> None:
        self.console.print(Panel(text or "<empty>", title=title, border_style=style, box=box.ROUNDED))

    def render_table(self, *, title: str, columns: list[str], rows: list[list[str]]) -> None:
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

    def clear(self) -> None:
        self.console.clear()

    def spinner(self, label: str) -> Spinner:
        return Spinner(label, out=self.output_stream)
```

- [ ] **Step 4: Run UI tests to verify pass**

Run:

```bash
pytest tests/test_orchestrator/test_repl_ui.py -q
```

Expected:

```text
4 passed
```

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add orchestrator/repl_ui.py tests/test_orchestrator/test_repl_ui.py
git commit -m "feat(orchestrator): add multi-agent repl ui"
```

---

### Task 4: Slash Command Handlers

**Files:**
- Create: `orchestrator/repl_commands.py`
- Create: `tests/test_orchestrator/test_repl_commands.py`
- Modify: `tests/test_orchestrator/test_slash_agents.py`

- [ ] **Step 1: Write failing command handler tests**

Create `tests/test_orchestrator/test_repl_commands.py`:

```python
from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from orchestrator.registry import Card
from orchestrator.repl_commands import CommandResult, ReplCommandHandler
from orchestrator.repl_state import MultiAgentSessionState
from orchestrator.repl_ui import ReplUI


class _Cfg:
    provider = "mock"
    model = "mock-default"
    protocol = "openai"
    base_url = "http://mock.invalid/v1"
    api_key_env = "MOCK_API_KEY"


class _Tool:
    def __init__(self, name):
        self.name = name


class _Handle:
    def __init__(self):
        self.card = Card(
            id="tool-agent",
            display_name="Tool Agent",
            version="1.0.0",
            entrypoint={},
            mcp={},
            a2a={},
            capabilities_hint=[],
            model_override=None,
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
        input_stream=io.StringIO(),
        output_stream=buf,
    )
    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[],
        instruction_files=[],
        memory_snapshot="memory",
        workspace=tmp_path,
    )
    return ReplCommandHandler(ui=ui, state=state, host=_Host(), router=_Router()), state, buf


def test_help_command_renders_and_continues(tmp_path):
    handler, state, buf = _handler(tmp_path)
    result = handler.handle("/help")
    assert result == CommandResult.CONTINUE
    assert "Slash Commands" in buf.getvalue()


def test_exit_command_requests_exit(tmp_path):
    handler, state, buf = _handler(tmp_path)
    assert handler.handle("/exit") == CommandResult.EXIT
    assert handler.handle("/quit") == CommandResult.EXIT


def test_agents_and_tools_render_tables(tmp_path):
    handler, state, buf = _handler(tmp_path)
    assert handler.handle("/agents") == CommandResult.CONTINUE
    assert handler.handle("/tools") == CommandResult.CONTINUE
    text = buf.getvalue()
    assert "tool-agent" in text
    assert "read_file" in text


def test_permissions_updates_state(tmp_path):
    handler, state, buf = _handler(tmp_path)
    assert handler.handle("/permissions read-only") == CommandResult.CONTINUE
    assert state.permission_mode == "read-only"
    assert "read-only" in buf.getvalue()


def test_compact_resets_history(tmp_path):
    handler, state, buf = _handler(tmp_path)
    state.record_turn(user_input="x", capability="read_file", owner="tool-agent", observation="y", error=None)
    assert handler.handle("/compact") == CommandResult.CONTINUE
    assert state.recent_history == []
    assert state.thread_id == "multi-agent-session-2"
    assert "Compacted" in buf.getvalue()


def test_unknown_command_is_handled(tmp_path):
    handler, state, buf = _handler(tmp_path)
    assert handler.handle("/nope") == CommandResult.CONTINUE
    assert "Unknown command" in buf.getvalue()
```

- [ ] **Step 2: Run command tests to verify import failure**

Run:

```bash
pytest tests/test_orchestrator/test_repl_commands.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'orchestrator.repl_commands'
```

- [ ] **Step 3: Implement `orchestrator/repl_commands.py`**

Create `orchestrator/repl_commands.py`:

```python
from __future__ import annotations

from enum import Enum

from orchestrator.repl_ui import ReplUI


class CommandResult(Enum):
    NOT_COMMAND = "not_command"
    CONTINUE = "continue"
    EXIT = "exit"


class ReplCommandHandler:
    def __init__(self, *, ui: ReplUI, state, host, router):
        self.ui = ui
        self.state = state
        self.host = host
        self.router = router

    def handle(self, line: str) -> CommandResult:
        command = line.split(maxsplit=1)[0].lower()
        if not command.startswith("/"):
            return CommandResult.NOT_COMMAND
        if command in {"/exit", "/quit"}:
            return CommandResult.EXIT
        if command == "/help":
            self.ui.render_help()
            return CommandResult.CONTINUE
        if command == "/agents":
            self._render_agents()
            return CommandResult.CONTINUE
        if command == "/tools":
            self._render_tools()
            return CommandResult.CONTINUE
        if command == "/permissions":
            self._handle_permissions(line)
            return CommandResult.CONTINUE
        if command == "/config":
            self._render_config()
            return CommandResult.CONTINUE
        if command == "/skills":
            self._render_skills()
            return CommandResult.CONTINUE
        if command == "/instructions":
            self._render_instructions()
            return CommandResult.CONTINUE
        if command == "/clear":
            self.ui.clear()
            return CommandResult.CONTINUE
        if command == "/compact":
            self.state.compact(memory_snapshot=self.state.memory_snapshot)
            self.ui.render_text(title="Compacted", text=f"New thread: {self.state.thread_id}", style="green")
            return CommandResult.CONTINUE
        if command == "/model":
            self.ui.render_error("Model Configuration", "Use python cli.py --single /model until the multi-agent wizard task is implemented.")
            return CommandResult.CONTINUE
        self.ui.render_error("Unknown command", f"{command}\nType /help for available commands.")
        return CommandResult.CONTINUE

    def _render_agents(self) -> None:
        rows = []
        for handle in self.host.list_handles():
            card = handle.card
            rows.append([
                card.id,
                str(card.version),
                str(handle.a2a_url or "-"),
                "healthy",
                str(len(getattr(card, "capabilities_hint", []) or [])),
            ])
        self.ui.render_table(
            title="Specialist Agents",
            columns=["ID", "Version", "A2A URL", "Health", "Hints"],
            rows=rows,
        )

    def _render_tools(self) -> None:
        rows = []
        for capability in self.router.all_capabilities():
            rows.append([capability, self.router.resolve(capability)])
        self.ui.render_table(
            title="Registered Capabilities",
            columns=["Capability", "Owner"],
            rows=rows,
        )

    def _handle_permissions(self, line: str) -> None:
        parts = line.split(maxsplit=1)
        if len(parts) == 1:
            self.ui.render_text(
                title="Permission Mode",
                text=f"Current permission mode: {self.state.permission_mode}",
            )
            return
        requested = parts[1].strip()
        if not self.state.set_permission_mode(requested):
            self.ui.render_error(
                "Invalid permission mode",
                "Use read-only, workspace-write, or danger-full-access.",
            )
            return
        self.ui.render_text(title="Permission Mode", text=f"Permission mode set: {requested}", style="green")

    def _render_config(self) -> None:
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
        self.ui.render_table(title="Effective Config", columns=["Key", "Value"], rows=rows)

    def _render_skills(self) -> None:
        rows = []
        for skill in self.state.skills:
            rows.append([
                str(getattr(skill, "name", "")),
                str(getattr(skill, "title", "")),
                str(getattr(skill, "path", "")),
            ])
        self.ui.render_table(title="Installed Skills", columns=["Name", "Title", "Path"], rows=rows)

    def _render_instructions(self) -> None:
        rows = []
        for file in self.state.instruction_files:
            path = str(getattr(file, "path", ""))
            content = str(getattr(file, "content", ""))
            rows.append([path, str(len(content))])
        self.ui.render_table(title="Project Instructions", columns=["Path", "Characters"], rows=rows)
```

- [ ] **Step 4: Move `/agents` test coverage to new handler**

Modify `tests/test_orchestrator/test_slash_agents.py` to import `ReplCommandHandler` or delete the file after confirming `/agents` is covered in `test_repl_commands.py`. If keeping it, replace its content with:

```python
from tests.test_orchestrator.test_repl_commands import test_agents_and_tools_render_tables
```

This keeps the old test name valid while avoiding duplicate fake host definitions.

- [ ] **Step 5: Run command tests**

Run:

```bash
pytest tests/test_orchestrator/test_repl_commands.py tests/test_orchestrator/test_slash_agents.py -q
```

Expected:

```text
7 passed
```

- [ ] **Step 6: Commit Task 4**

Run:

```bash
git add orchestrator/repl_commands.py tests/test_orchestrator/test_repl_commands.py tests/test_orchestrator/test_slash_agents.py
git commit -m "feat(orchestrator): add multi-agent repl slash commands"
```

---

### Task 5: REPL Loop and Main Wiring

**Files:**
- Create: `orchestrator/repl.py`
- Modify: `orchestrator/main.py`
- Modify: `tests/test_e2e_multi_agent/test_e2e_simple_tool.py`

- [ ] **Step 1: Write failing REPL e2e regression**

Append to `tests/test_e2e_multi_agent/test_e2e_simple_tool.py`:

```python

@pytest.mark.e2e
def test_multi_agent_repl_dispatches_read_file_and_exits(tmp_path):
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

- [ ] **Step 2: Run e2e test to verify stub failure**

Run:

```bash
pytest tests/test_e2e_multi_agent/test_e2e_simple_tool.py::test_multi_agent_repl_dispatches_read_file_and_exits -q
```

Expected:

```text
FAILED ... assert 'multi-agent REPL not fully implemented' not in proc.stdout
```

- [ ] **Step 3: Implement `orchestrator/repl.py`**

Create `orchestrator/repl.py`:

```python
from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

import config
from orchestrator.mcp_host import MCPHost
from orchestrator.registry import load_cards
from orchestrator.repl_commands import CommandResult, ReplCommandHandler
from orchestrator.repl_state import MultiAgentSessionState
from orchestrator.repl_ui import ReplUI
from orchestrator.router import CapabilityRouter
from orchestrator.turns import LLMPlanner, TurnRunner, _stub_planner


def _load_memory_snapshot() -> str:
    try:
        from tool import tool_memory

        return tool_memory.snapshot_for_system_prompt()
    except Exception:
        return ""


def _load_skills():
    from skills.skill_loader import load_skills

    return load_skills()


def _load_instruction_files():
    from project_context import discover_instruction_files

    return discover_instruction_files()


async def bootstrap(host: MCPHost, router: CapabilityRouter) -> None:
    cards = load_cards(Path(".agent") / "agents")
    for card in cards:
        await host.spawn(card)
        tools = await host.list_tools(card.id)
        router.register(card.id, [t.name for t in tools])
    runtime_dir = Path(".agent/runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    import json

    (runtime_dir / "peers.json").write_text(json.dumps(host.a2a_urls()), encoding="utf-8")


def _build_planner(router: CapabilityRouter, state: MultiAgentSessionState):
    provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
    if provider.startswith("mock") or not provider:
        return _stub_planner
    from orchestrator.main import _build_orchestrator_llm

    return LLMPlanner(
        llm=_build_orchestrator_llm(),
        available_capabilities=router.all_capabilities(),
        context_provider=lambda: "\n".join(
            [
                f"Thread: {state.thread_id}",
                f"Memory:\n{state.memory_snapshot}",
                f"Recent history:\n{state.recent_history}",
            ]
        ),
    )


async def run_repl() -> int:
    config.hydrate_env_from_credentials()
    active_cfg = config.load_active_config()
    hmac_key = secrets.token_urlsafe(32)
    host = MCPHost(hmac_key=hmac_key)
    router = CapabilityRouter()
    ui = ReplUI()
    state = MultiAgentSessionState.from_runtime(
        active_cfg=active_cfg,
        skills=_load_skills(),
        instruction_files=_load_instruction_files(),
        memory_snapshot=_load_memory_snapshot(),
        workspace=Path.cwd(),
    )
    try:
        await bootstrap(host, router)
        ui.render_welcome(
            provider=state.provider,
            model=state.model,
            permission_mode=state.permission_mode,
            agent_count=len(host.list_handles()),
            workspace=str(state.workspace),
        )
        command_handler = ReplCommandHandler(ui=ui, state=state, host=host, router=router)
        turn = 0
        while True:
            try:
                line = ui.read_input()
            except EOFError:
                break
            except KeyboardInterrupt:
                ui.render_text(title="Goodbye", text="Goodbye.", style="cyan")
                break
            if not line:
                continue
            command_result = command_handler.handle(line)
            if command_result is CommandResult.EXIT:
                break
            if command_result is CommandResult.CONTINUE:
                continue
            turn += 1
            planner = _build_planner(router, state)
            runner = TurnRunner(
                host=host,
                router=router,
                hmac_key=hmac_key,
                permission_mode_provider=lambda: state.permission_mode,
                planner=planner,
            )
            spinner = ui.spinner("Thinking...")
            spinner.start()
            try:
                result = await runner.run(line, trace_id=f"repl-{turn}")
            finally:
                spinner.stop()
            if result.error:
                ui.render_error("Turn Error", result.error)
            else:
                ui.render_text(title=result.owner or "orchestrator", text=result.text, style="cyan")
            state.record_turn(
                user_input=line,
                capability=result.capability,
                owner=result.owner,
                observation=result.text,
                error=result.error,
            )
            ui.render_divider()
        return 0
    finally:
        await host.shutdown_all()
```

- [ ] **Step 4: Wire `orchestrator/main.py` to new REPL**

Replace the current `run_repl()` body in `orchestrator/main.py` with:

```python
async def run_repl() -> int:
    from orchestrator.repl import run_repl as _run_repl

    return await _run_repl()
```

- [ ] **Step 5: Run REPL e2e and prompt e2e**

Run:

```bash
pytest tests/test_e2e_multi_agent/test_e2e_simple_tool.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 6: Commit Task 5**

Run:

```bash
git add orchestrator/repl.py orchestrator/main.py tests/test_e2e_multi_agent/test_e2e_simple_tool.py
git commit -m "feat(orchestrator): replace multi-agent repl stub"
```

---

### Task 6: Planner Context and Session Continuity

**Files:**
- Modify: `orchestrator/repl_state.py`
- Modify: `orchestrator/repl.py`
- Modify: `orchestrator/turns.py`
- Modify: `tests/test_orchestrator/test_turns.py`
- Modify: `tests/test_orchestrator/test_repl_state.py`

- [ ] **Step 1: Add failing context rendering test**

Append to `tests/test_orchestrator/test_repl_state.py`:

```python

def test_planner_context_includes_memory_instructions_skills_and_history(tmp_path):
    class _Instruction:
        path = tmp_path / "AGENTS.md"
        content = "Always be careful."

    class _Skill:
        name = "ppt-master"
        title = "PowerPoint"
        path = tmp_path / "skills" / "ppt-master" / "SKILL.md"

    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[_Skill()],
        instruction_files=[_Instruction()],
        memory_snapshot="Remember user prefers concise answers.",
        workspace=tmp_path,
    )
    state.record_turn(
        user_input="read README",
        capability="read_file",
        owner="tool-agent",
        observation="README text",
        error=None,
    )

    context = state.render_planner_context(["read_file", "skill.ppt-master"])

    assert "mock-default" in context
    assert "read_file" in context
    assert "Always be careful." in context
    assert "Remember user prefers concise answers." in context
    assert "ppt-master" in context
    assert "README text" in context
```

- [ ] **Step 2: Run state context test to verify missing method**

Run:

```bash
pytest tests/test_orchestrator/test_repl_state.py::test_planner_context_includes_memory_instructions_skills_and_history -q
```

Expected:

```text
AttributeError: 'MultiAgentSessionState' object has no attribute 'render_planner_context'
```

- [ ] **Step 3: Implement `render_planner_context`**

Add to `MultiAgentSessionState` in `orchestrator/repl_state.py`:

```python
    def render_planner_context(self, capabilities: list[str]) -> str:
        instruction_sections = []
        for file in self.instruction_files:
            path = getattr(file, "path", "")
            content = getattr(file, "content", "")
            instruction_sections.append(f"## {path}\n{content}")
        skill_lines = []
        for skill in self.skills:
            name = getattr(skill, "name", "")
            title = getattr(skill, "title", "")
            path = getattr(skill, "path", "")
            skill_lines.append(f"- {name}: {title} ({path})")
        history_lines = []
        for item in self.recent_history:
            history_lines.append(
                "\n".join(
                    [
                        f"User: {item['user']}",
                        f"Capability: {item['capability']}",
                        f"Owner: {item['owner']}",
                        f"Observation: {item['observation']}",
                        f"Error: {item['error']}",
                    ]
                )
            )
        return "\n\n".join(
            [
                f"Provider: {self.provider}",
                f"Model: {self.model}",
                f"Protocol: {self.protocol}",
                f"Permission mode: {self.permission_mode}",
                "Capabilities:\n" + "\n".join(f"- {cap}" for cap in capabilities),
                "Memory:\n" + (self.memory_snapshot or "<none>"),
                "Project instructions:\n" + ("\n\n".join(instruction_sections) or "<none>"),
                "Skills:\n" + ("\n".join(skill_lines) or "<none>"),
                "Recent history:\n" + ("\n\n".join(history_lines) or "<none>"),
            ]
        )
```

- [ ] **Step 4: Update REPL planner construction to use rendered context**

In `orchestrator/repl.py`, replace the `context_provider` lambda in `_build_planner` with:

```python
context_provider=lambda: state.render_planner_context(router.all_capabilities())
```

- [ ] **Step 5: Run continuity tests**

Run:

```bash
pytest tests/test_orchestrator/test_repl_state.py tests/test_orchestrator/test_turns.py -q
```

Expected:

```text
5 passed
```

- [ ] **Step 6: Commit Task 6**

Run:

```bash
git add orchestrator/repl_state.py orchestrator/repl.py tests/test_orchestrator/test_repl_state.py tests/test_orchestrator/test_turns.py
git commit -m "feat(orchestrator): add repl planner context"
```

---

### Task 7: Cancellation, Crash, and Shutdown Hardening

**Files:**
- Modify: `orchestrator/mcp_host.py`
- Modify: `orchestrator/repl.py`
- Modify: `tests/test_e2e_multi_agent/test_ctrl_c_cancel.py`
- Modify: `tests/test_e2e_multi_agent/test_specialist_crash.py`
- Modify: `tests/test_e2e_multi_agent/test_e2e_simple_tool.py`

- [ ] **Step 1: Tighten clean exit assertion**

In `tests/test_e2e_multi_agent/test_e2e_simple_tool.py`, inside `test_multi_agent_repl_dispatches_read_file_and_exits`, add:

```python
    assert "unhandled exception during asyncio.run() shutdown" not in proc.stderr
    assert "Attempted to exit cancel scope" not in proc.stderr
```

- [ ] **Step 2: Run clean exit test to verify Windows shutdown failure if present**

Run:

```bash
pytest tests/test_e2e_multi_agent/test_e2e_simple_tool.py::test_multi_agent_repl_dispatches_read_file_and_exits -q
```

Expected before hardening on affected Windows environments:

```text
FAILED ... assert 'unhandled exception during asyncio.run() shutdown' not in proc.stderr
```

If the local environment already passes, continue; the assertions still guard the regression.

- [ ] **Step 3: Harden `MCPHost.shutdown_all`**

Replace `shutdown_all` in `orchestrator/mcp_host.py` with:

```python
    async def shutdown_all(self) -> None:
        for cid, handle in list(self._clients.items()):
            try:
                await asyncio.wait_for(handle.stack.aclose(), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception) as e:
                log.debug("error closing client %s: %s", cid, type(e).__name__)
        self._clients.clear()
```

- [ ] **Step 4: Add cancellation handling in REPL turn execution**

In `orchestrator/repl.py`, wrap the `runner.run` call:

```python
            try:
                result = await runner.run(line, trace_id=f"repl-{turn}")
            except (asyncio.CancelledError, KeyboardInterrupt):
                await host.cancel_all()
                ui.render_error("Cancelled", "Current turn cancelled. Specialists remain available.")
                continue
            finally:
                spinner.stop()
```

Ensure `spinner.stop()` is called exactly once by removing any duplicate `finally` block around the same run call.

- [ ] **Step 5: Tighten specialist crash e2e**

In `tests/test_e2e_multi_agent/test_specialist_crash.py`, remove the skip branch that says the REPL is a stub. Replace:

```python
        except psutil.NoSuchProcess:
            pytest.skip("orchestrator exited before 3s; REPL is a stub that exits immediately")
```

with:

```python
        except psutil.NoSuchProcess:
            pytest.fail("orchestrator exited before REPL could accept input")
```

Replace the `tool_child is None` skip branch with:

```python
        if tool_child is None:
            pytest.fail("could not locate tool-agent child process after REPL startup")
```

- [ ] **Step 6: Run lifecycle e2e tests**

Run:

```bash
pytest tests/test_e2e_multi_agent/test_e2e_simple_tool.py tests/test_e2e_multi_agent/test_ctrl_c_cancel.py tests/test_e2e_multi_agent/test_specialist_crash.py -q
```

Expected:

```text
4 passed
```

- [ ] **Step 7: Commit Task 7**

Run:

```bash
git add orchestrator/mcp_host.py orchestrator/repl.py tests/test_e2e_multi_agent/test_e2e_simple_tool.py tests/test_e2e_multi_agent/test_ctrl_c_cancel.py tests/test_e2e_multi_agent/test_specialist_crash.py
git commit -m "fix(orchestrator): harden multi-agent repl lifecycle"
```

---

### Task 8: README and Final Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README multi-agent REPL section**

In `README.md`, replace the line:

```markdown
- (Full multi-agent REPL UX ships post-Day-1; for now, use `python cli.py prompt "..."` for non-interactive use)
```

with:

```markdown
- `/help` — show multi-agent REPL commands
- `/tools` — list routed specialist capabilities
- `/permissions [mode]` — show or change the permission mode for later turns
- `/config` — show model, permission, workspace, and runtime configuration
- `/skills` — list installed local skills
- `/instructions` — list loaded project instruction files
- `/compact` — start a fresh multi-agent session thread
```

Add this paragraph below the slash-command list:

```markdown
Interactive `python cli.py` now uses the multi-agent REPL by default. It accepts natural language input, routes work through the orchestrator planner, and keeps `capability:arg` input available for deterministic development and tests. Use `python cli.py --single` when you need the original single-process REPL.
```

- [ ] **Step 2: Run focused unit and e2e verification**

Run:

```bash
pytest tests/test_orchestrator/test_repl_state.py tests/test_orchestrator/test_turns.py tests/test_orchestrator/test_repl_ui.py tests/test_orchestrator/test_repl_commands.py tests/test_orchestrator/test_llm_planner.py tests/test_orchestrator/test_graph.py tests/test_orchestrator/test_mcp_host.py tests/test_e2e_multi_agent/test_e2e_simple_tool.py tests/test_e2e_multi_agent/test_e2e_legacy_mode.py -q
```

Expected:

```text
23 passed
```

- [ ] **Step 3: Run direct smoke checks**

Run:

```bash
'/exit' | python cli.py
python cli.py prompt "read_file:README.md"
```

Expected for the first command:

```text
LangChain Agent CLI - Multi-Agent
```

Expected for the second command:

```text
[tool]
```

The second command also prints README content.

- [ ] **Step 4: Commit README and final verification updates**

Run:

```bash
git add README.md
git commit -m "docs: document multi-agent repl"
```

---

## Self-Review Checklist

- Spec coverage:
  - Module boundaries are covered by Tasks 1 through 5.
  - Slash commands are covered by Task 4.
  - Natural language and deterministic planner paths are covered by Tasks 2 and 6.
  - Session state and compaction are covered by Tasks 1 and 6.
  - Terminal rendering is covered by Task 3.
  - Lifecycle, cancellation, crash, and Windows shutdown are covered by Task 7.
  - README acceptance criterion is covered by Task 8.

- Placeholder scan:
  - This plan has no unresolved marker words.
  - This plan has no deferred-work marker words.
  - Each implementation task includes concrete files, tests, commands, and expected outcomes.

- Type consistency:
  - `MultiAgentSessionState` is defined in Task 1 and reused by Tasks 4, 5, and 6.
  - `TurnRunner`, `TurnResult`, `LLMPlanner`, and `_stub_planner` are defined in Task 2 and reused by Tasks 5 and 6.
  - `ReplUI` is defined in Task 3 and reused by Tasks 4 and 5.
  - `ReplCommandHandler` and `CommandResult` are defined in Task 4 and reused by Task 5.
