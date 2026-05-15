# Multi-Agent REPL Design

Date: 2026-05-15

## Goal

Make `python cli.py` start a production-shaped multi-agent REPL instead of the
current Phase 5 placeholder. The REPL should feel close to the legacy terminal
experience while using the new orchestrator, MCP specialists, A2A telemetry, and
capability routing as its runtime.

The first complete version should support natural language input, common slash
commands, legacy-like session continuity, and polished terminal rendering. The
legacy `--single` path remains unchanged.

## Non-Goals

- Do not refactor legacy `CliApp` into shared runtime modules in this pass.
- Do not make `skill-agent` or `tool-agent` responsible for long-lived chat
  history.
- Do not implement `/agents reload` in the first version.
- Do not require exact ANSI snapshot matching in tests.

## Chosen Approach

Use an independent multi-agent REPL with a small terminal UI adapter layer.

Legacy code is a reference for behavior and visual style, not a runtime
dependency. The multi-agent path may reuse lower-level modules such as
`config.py`, `skills.skill_loader`, `project_context.py`, and `tool.tool_memory`,
but it should not instantiate or call `legacy.single_agent_loop.CliApp`.

## Module Boundaries

`orchestrator/main.py`

- Stays thin.
- Dispatches `main(prompt=None)` to either one-shot prompt execution or the REPL.
- Does not hold REPL command, rendering, or session state details.

`orchestrator/turns.py`

- Owns one-turn orchestration.
- Shared by `python cli.py prompt ...` and interactive REPL mode.
- Bootstrapped with `MCPHost`, `CapabilityRouter`, permission mode, planner
  configuration, session context, and a renderer callback.
- Returns a structured turn result containing capability, owner agent, rendered
  text blocks, errors, and telemetry observations.

`orchestrator/repl.py`

- Owns the interactive REPL loop.
- Starts and shuts down specialists.
- Reads user input through `repl_ui`.
- Delegates slash commands to `repl_commands`.
- Delegates normal user turns to `turns`.
- Updates `MultiAgentSessionState` after each turn.

`orchestrator/repl_ui.py`

- Owns terminal presentation.
- Recreates the legacy terminal feel: boxed prompt, prompt history, slash
  completion, welcome panel, spinners, Rich panels/tables, compact result panels,
  error panels, and dim dividers.
- Provides non-TTY fallbacks for automated tests.
- Keeps ANSI-heavy details out of orchestration logic.

`orchestrator/repl_commands.py`

- Owns multi-agent slash command handling.
- Implements the selected common command set independently of legacy `CliApp`.
- Uses lower-level config, skills, instruction, and tool modules where useful.

`orchestrator/repl_state.py`

- Defines `MultiAgentSessionState`.
- Tracks model config, permission mode, thread/session counters, recent history,
  loaded skills, project instructions, memory snapshot, and last error.

## Slash Commands

The first full version supports:

- `/help`
- `/exit`
- `/quit`
- `/agents`
- `/tools`
- `/permissions [mode]`
- `/config`
- `/model [provider]`
- `/skills`
- `/instructions`
- `/clear`
- `/compact`

Command behavior:

- `/help` renders a multi-agent command table in the legacy visual style.
- `/exit` and `/quit` perform graceful specialist shutdown and exit the REPL.
- `/agents` lists specialist id, version, A2A URL, health, and capability count.
- `/tools` lists registered router capabilities with their owning agent.
- `/permissions [mode]` reads or updates the REPL session permission mode. The
  new mode is used for later turns in the same process.
- `/config` shows active model config, permission mode, workspace, runtime
  directory, and agent count.
- `/model [provider]` recreates the legacy wizard experience while using
  `config.py` provider registry, settings, and credentials helpers.
- `/skills` lists the local skills catalog via `skills.skill_loader.load_skills()`.
- `/instructions` lists discovered project instruction files via
  `project_context.discover_instruction_files()`.
- `/clear` clears the terminal.
- `/compact` starts a fresh conversation thread for later turns, resets recent
  context, increments compaction counters, and reloads the memory snapshot.

Unknown slash commands render a legacy-style "Unknown command" message and point
the user to `/help`.

## Natural Language Turns

Normal user input should feel like the legacy assistant:

- Users type natural language by default.
- The orchestrator planner chooses a capability and arguments.
- The existing `capability:arg` parser remains as a deterministic development
  and test escape hatch.
- `MOCK_ORCH_SCRIPT` remains available for scripted tests.

Planner prompt context should include:

- Active provider/model/protocol.
- Current permission mode.
- Registered capabilities and owning agents.
- Project instructions.
- Memory snapshot from `tool_memory.snapshot_for_system_prompt()`.
- Skills catalog.
- Recent multi-agent REPL history.
- Recent observations from tool/skill execution.

The planner still returns strict JSON:

```json
{"capability": "<name>", "arguments": {}}
```

If the planner returns invalid JSON, the REPL shows an error panel and returns to
the next input prompt.

## Session State and Memory

`MultiAgentSessionState` tracks:

- `provider`
- `model`
- `protocol`
- `base_url`
- `api_key_env`
- `permission_mode`
- `thread_id`
- `turns`
- `tool_calls`
- `compacted_turns`
- `seen_messages`
- `last_error`
- `recent_history`
- `memory_snapshot`
- `instruction_files`
- `skills`

Continuity is maintained at the orchestrator layer. Specialists remain mostly
stateless. The orchestrator stores recent user inputs, planner decisions,
specialist observations, and assistant-facing summaries, then injects the useful
parts into later planner prompts.

For the first version of `/compact`, clearing recent history is acceptable. LLM
summarization can be added later if needed.

## Terminal Rendering

`repl_ui.py` should closely match the legacy REPL's terminal feel.

Input:

- Boxed prompt.
- `prompt_toolkit` history.
- Slash command completion from the multi-agent command table.
- Non-TTY fallback using `stdin.readline()`.

Startup:

- Welcome panel with provider/model, permission mode, workspace, and started
  agent count.
- Clear indication that this is multi-agent mode.

Turn output:

- Spinner while planning and while calling specialists.
- Spinner labels like `Calling tool-agent: read_file` and
  `Calling skill-agent: skill.ppt-master`.
- Specialist output formatted through the UI layer, not emitted as raw log lines.
- Optional dim metadata labels such as `[tool]` and `[skill]`.
- Compact result panels for tool or skill outputs.
- Dim divider after each completed turn.

Tables and panels:

- `/help`, `/status` if added later, `/config`, `/tools`, `/skills`,
  `/instructions`, and `/agents` use Rich tables or panels with legacy-like
  colors, density, and borders.

Errors:

- Planner parse errors, unknown capabilities, permission denials, specialist
  crashes, and unexpected exceptions render as red error panels.
- Errors do not exit the REPL unless they happen during startup before any
  usable specialist network exists.

## Lifecycle and Failure Handling

Startup:

1. Read `.agent/agents/*.card.json`.
2. Start each specialist through `MCPHost`.
3. Collect `list_tools()` from each specialist.
4. Register capabilities in `CapabilityRouter`.
5. Write `.agent/runtime/peers.json`.
6. Start telemetry tailing for A2A events.
7. Render the welcome panel and enter the input loop.

Turn execution:

1. Allocate a unique `trace_id`.
2. Build planner context from session state.
3. Select capability and arguments.
4. Sign permission grant with `PermissionGate`.
5. Call the owning specialist via MCP.
6. Collect result and telemetry observations.
7. Render output.
8. Update session history and counters.

Ctrl+C:

- Ctrl+C while input is focused exits the REPL.
- Ctrl+C during a turn cancels the turn, calls `host.cancel_all()`, stops current
  telemetry tailing, shows a cancelled panel, and returns to the input prompt.
- Specialists should not be killed by ordinary cancellation.

Specialist crash:

- `host.call_tool()` returns a structured error rather than allowing the REPL to
  crash.
- `/agents` marks the specialist unhealthy.
- A later call to the same agent may attempt one respawn. If respawn fails, show
  a clear error and keep the REPL alive.

Shutdown:

- `/exit`, `/quit`, EOF, and clean application shutdown use the same path.
- Stop telemetry tailing.
- Close MCP sessions.
- Suppress known Windows anyio stdio cleanup noise at the host boundary.
- Do not delete `.agent/runtime` files; they are useful for diagnostics.

## Testing Strategy

Stage 1: REPL skeleton and shared turn runner

- Add a regression test for `python cli.py` with stdin:
  `read_file:<path>\n/exit\n`.
- Assert tool output appears and no traceback is written.
- Verify `python cli.py prompt ...` still works through the same turn runner.

Stage 2: UI adapter

- Unit-test pure formatting helpers where possible.
- E2E tests assert important text and behavior, not exact ANSI.
- Verify non-TTY fallback for test subprocesses.

Stage 3: Slash commands

- Unit-test command handlers with fake state, fake host, and string buffers.
- Cover `/help`, `/agents`, `/tools`, `/permissions`, `/config`, `/skills`,
  `/instructions`, `/compact`, and unknown commands.
- Cover `/model` through narrower tests around provider/config selection helpers,
  avoiding brittle interactive keystroke tests.

Stage 4: Session continuity

- Test that recent observations enter planner context on the next turn.
- Test `/compact` resets recent history and updates `thread_id`/counters.

Stage 5: cancellation, crash, and shutdown

- E2E Ctrl+C during a long turn returns to the REPL.
- E2E specialist crash does not terminate the REPL.
- E2E `/exit` exits without Windows anyio shutdown traceback.

## Implementation Order

1. Extract shared turn execution into `orchestrator/turns.py`.
2. Add `repl_state.py`.
3. Add `repl_ui.py` with non-TTY fallback first, then interactive prompt polish.
4. Add `repl_commands.py` for the common command set.
5. Replace the Phase 5 `run_repl()` placeholder with `orchestrator.repl.run_repl`.
6. Add session context injection to planner prompts.
7. Add cancellation/crash/shutdown hardening.
8. Update README to remove the "Full multi-agent REPL UX ships post-Day-1"
   caveat once the feature is complete.

## Acceptance Criteria

- `python cli.py` enters a multi-agent REPL rather than printing the Phase 5
  placeholder.
- Natural language input routes through the orchestrator planner.
- `capability:arg` still works for deterministic tests.
- The selected common slash commands work in multi-agent mode.
- The terminal looks and feels close to the legacy REPL for both input and
  output.
- Recent conversation context influences later planner decisions.
- `/compact` starts a fresh conversation thread.
- Ctrl+C during a turn cancels that turn without killing specialists.
- Specialist crash reports clearly and returns to the prompt.
- `/exit` shuts down cleanly, including on Windows.
- `python cli.py --single` remains unchanged.
