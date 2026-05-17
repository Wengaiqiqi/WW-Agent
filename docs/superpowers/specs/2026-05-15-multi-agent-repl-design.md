# Multi-Agent REPL Design

Date: 2026-05-15 (updated 2026-05-15 — refined interface, data flow, error handling, lifecycle)

## Goal

Make `python cli.py` start a production-shaped multi-agent REPL instead of the
current Phase 5 placeholder. The REPL should feel close to the legacy terminal
experience while using the new orchestrator, MCP specialists, A2A telemetry, and
capability routing as its runtime.

## Non-Goals

- Do not refactor legacy `CliApp` into shared runtime modules in this pass.
- Do not make `skill-agent` or `tool-agent` responsible for long-lived chat history.
- Do not implement `/agents reload` in the first version.
- Do not require exact ANSI snapshot matching in tests.

## Chosen Approach

Use `REPLController` + `ReplCommandHandler` + `ReplUI`, coordinated through a
thin `run_repl()` in `main.py`. Legacy code is a reference, not a runtime
dependency.

## Module Boundaries

### `orchestrator/repl_types.py` (NEW)

Shared small module with zero intra-orchestrator imports:

```python
from enum import Enum, auto

class LoopAction(Enum):
    CONTINUE = auto()
    EXIT = auto()
```

Used by both `REPLController` and `ReplCommandHandler` without circular imports.

### `orchestrator/repl_controller.py` (NEW)

Core REPL orchestration. Depends on `repl_types`, `turns`, `repl_state`, `mcp_host`, `router`.

```
REPLController
  ├── handle_input(text: str) -> LoopAction   (async)
  ├── _execute_turn(text: str) -> LoopAction  (async)
  ├── _ensure_planner() -> None               (async, lazy)
  └── _is_fatal(error: Exception) -> bool
```

Constructor: `REPLController(*, host, router, hmac_key, state, commands, ui)`

- `handle_input`: if text starts with `/` → delegate to `commands.handle()`, else `_execute_turn`
- `_ensure_planner`: lazy init; `context_provider=lambda: state.render_planner_context(router.all_capabilities())` — dynamic, not frozen at init time
- `_is_fatal`: specialist network error, no capabilities, host unavailable → fatal; everything else recoverable

### `orchestrator/repl_commands.py` (NEW)

Slash command handler. Depends on `repl_types`, `repl_state`, `repl_ui`, `mcp_host`, `router`.

```
ReplCommandHandler(ui, state, host, router)
  ├── handle(text: str) -> LoopAction
  ├── _cmd_help(), _cmd_exit(), _cmd_quit()
  ├── _cmd_agents(), _cmd_tools(), _cmd_permissions(args)
  ├── _cmd_config(), _cmd_model(args), _cmd_skills()
  ├── _cmd_instructions(), _cmd_clear(), _cmd_compact()
```

- Only `/exit` and `/quit` return `EXIT`; all others return `CONTINUE`
- `hmac_key` not passed unless a specific command needs it
- All `_cmd_*` catch `Exception` (not `BaseException` — don't swallow `KeyboardInterrupt`, `SystemExit`)

### `orchestrator/repl_ui.py` (NEW)

Terminal presentation. Depends on `rich` and `prompt_toolkit` (optional).

```
ReplUI(*, console=None, input_stream=None, output_stream=None)
  ├── read_input() -> str            (sync, prompt_toolkit or fallback)
  ├── read_input_async() -> str      (async, prompt_toolkit or fallback)
  ├── read_input_async() -> str       (prompt_toolkit, with history + slash completion)
  ├── render_welcome(state)           (provider/model/permission/workspace/agent count)
  ├── render_goodbye()
  ├── render_cancelled()
  ├── render_table(rows, title)
  ├── render_error(message)
  ├── render_warning(message)
  ├── render_panel(content, title)
  ├── render_spinner(label)
  └── render_divider()
```

Non-TTY fallback: `read_input_async()` falls back to `sys.stdin.readline()`.

### `orchestrator/main.py` (MODIFIED)

Stays thin: bootstrap → construct dependencies → loop → shutdown.

- `run_repl()` replaces the Phase 5 placeholder
- `run_prompt()` unchanged

### `orchestrator/turns.py` (EXISTING, possibly minor additions)

- `TurnRunner` — unchanged
- Add `render_planner_context(capabilities)` to `MultiAgentSessionState` in `repl_state.py`

### `orchestrator/repl_state.py` (EXISTING, minor additions)

- Add `render_planner_context(capabilities: list[str]) -> str` method
- Aggregates: provider, model, permission_mode, capabilities, skills, instruction_files, memory_snapshot, recent_history

## Slash Commands

| Command | Behavior |
|---------|----------|
| `/help` | Multi-agent command table |
| `/exit`, `/quit` | Graceful shutdown → `LoopAction.EXIT` |
| `/agents` | List specialist id, version, A2A URL, health, capability count |
| `/tools` | Registered capabilities with owning agent |
| `/permissions [mode]` | Read or update permission mode |
| `/config` | Active config, permission mode, workspace, runtime dir, agent count |
| `/model [provider]` | Legacy-like model selection wizard via config.py |
| `/skills` | Catalog from `skills.skill_loader.load_skills()` |
| `/instructions` | Project instruction files from `project_context` |
| `/clear` | Clear terminal |
| `/compact` | Fresh thread: reset recent_history, increment compacted_turns, reload memory_snapshot. If memory reload fails, keep old snapshot + print warning. |

Unknown commands: "Unknown command. Type /help for available commands."

## Natural Language Turn Data Flow

```
user input → REPLController.handle_input()
  → _execute_turn(text)
    1. trace_id = secrets.token_hex(4)
    2. _ensure_planner()  (first call: build LLMPlanner with dynamic context_provider)
    3. Pre-check: zero capabilities or all specialists unhealthy → fatal if normal turn
       (slash commands like /config, /agents, /exit still work)
    4. TurnRunner.run(user_input=text, trace_id=trace_id)
         → graph.ainvoke → plan → dispatch → result
    5. state.record_turn(...)  (always, even on error — planner learns from failures)
    6. render output or error panel
    7. return LoopAction.CONTINUE
```

If `TurnRunner.run()` raises (instead of returning `TurnResult(error=...)`):
- Catch, classify with `_is_fatal()`
- recoverable → red error panel + `CONTINUE`
- fatal → fatal error panel + `EXIT`

### Planner Context

`context_provider=lambda: state.render_planner_context(router.all_capabilities())`

Fresh each turn, pulling latest: provider, model, protocol, permission_mode, capabilities, instructions, memory_snapshot, skills, recent_history (last 12 turns including failures).

## Error Handling

### Tiered Model

| Severity | Examples | Action |
|----------|----------|--------|
| Recoverable | routing miss, planner parse error, permission denied, tool error, single specialist crash | Red panel, `record_turn(error=...)`, `CONTINUE` |
| Turn-blocking | Zero capabilities, all specialists unhealthy | Fatal for normal turns; slash commands still work |
| Fatal | MCP host corruption, startup network failure | Clear error message, `EXIT` |

### Specific Rules

- `_ensure_planner()` failure (bad config, missing key): recoverable — render config error, `CONTINUE`, user can `/config`/`/model`/`/exit`
- `asyncio.CancelledError`: user cancel during turn → recoverable, `host.cancel_all()`, `CONTINUE`; outer shutdown → let propagate
- Slash commands catch `Exception`, not `BaseException` (don't swallow `KeyboardInterrupt`, `SystemExit`)
- All `_cmd_*` methods must self-contain errors — never propagate to input loop
- Ctrl+C at input prompt → `EXIT`

## Lifecycle

### Startup

```
try:
  1. hmac_key = secrets.token_urlsafe(32)
  2. host = MCPHost(hmac_key=hmac_key)
  3. router = CapabilityRouter()
  4. mux = StreamMux()
  5. telemetry.reset_log()
  6. stop_telemetry = asyncio.Event()
  7. tail_task = asyncio.create_task(telemetry.tail(mux, stop_telemetry))
  8. _bootstrap(host, router)           ← captured early so specialist startup logs aren't missed
  9. config.hydrate_env_from_credentials()
  10. active_cfg = config.load_active_config()
  11. state = MultiAgentSessionState.from_runtime(
        active_cfg=active_cfg, skills=..., instruction_files=...,
        memory_snapshot=..., workspace=Path.cwd())
  12. ui = ReplUI(mux=mux)
  13. commands = ReplCommandHandler(ui=ui, state=state, host=host, router=router)
  14. controller = REPLController(host=host, router=router, hmac_key=hmac_key,
                                  state=state, commands=commands, ui=ui)
  15. ui.render_welcome(state)
  16. input loop
finally:
  shutdown (always runs)
```

### Shutdown

Independent cleanup, both protected:

```
  stop_telemetry.set()
  try:
      await asyncio.wait_for(tail_task, timeout=2.0)
  except asyncio.TimeoutError:
      tail_task.cancel()
      try: await tail_task
      except asyncio.CancelledError: pass

  await host.shutdown_all()   ← always runs, even if telemetry cleanup fails
```

### Ctrl+C Strategy

| Context | Behavior |
|---------|----------|
| Idle at input prompt | Exit REPL (`EXIT`) |
| During turn execution | Cancel turn, `host.cancel_all()`, render "Cancelled", `CONTINUE` |
| During slash command | Cancel, return to prompt |

### Partial Bootstrap

If `_bootstrap()` starts some specialists then fails mid-way, `host.shutdown_all()` in the `finally` block cleans up already-spawned child processes.

## Testing Strategy

Stage 1: REPL skeleton and shared turn runner
- Regression test: `python cli.py` with stdin `read_file:<path>\n/exit\n`
- Verify `python cli.py prompt ...` still works

Stage 2: UI adapter
- Unit-test formatting helpers
- E2E: assert important text, not exact ANSI
- Verify non-TTY fallback

Stage 3: Slash commands
- Unit-test command handlers with fake state, host, string buffer for ui
- Cover all commands + unknown command
- Narrower tests for `/model` helpers

Stage 4: Session continuity
- Recent observations enter planner context on next turn
- `/compact` resets history and updates thread_id/counters

Stage 5: Cancellation, crash, and shutdown
- Ctrl+C during long turn returns to prompt
- Specialist crash does not terminate REPL
- `/exit` exits without Windows anyio shutdown traceback

## Acceptance Criteria

- `python cli.py` enters multi-agent REPL (no Phase 5 placeholder)
- Natural language input routes through orchestrator planner
- `capability:arg` still works for deterministic tests
- All slash commands work in multi-agent mode
- Terminal looks/feels close to legacy REPL
- Recent conversation context influences later planner decisions
- `/compact` starts fresh thread
- Ctrl+C during turn cancels that turn, does not kill specialists
- Specialist crash reports clearly, returns to prompt
- `/exit` shuts down cleanly (including Windows)
- `python cli.py --single` unchanged
