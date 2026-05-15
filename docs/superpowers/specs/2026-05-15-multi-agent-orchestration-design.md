# Multi-Agent Orchestration Redesign

**Date:** 2026-05-15
**Status:** Draft — pending user review
**Author:** brainstormed via `superpowers:brainstorming`

---

## 1. Goals & Motivation

Restructure the current single-agent CLI into a multi-process agent network so that:

- **Skill execution** and **tool execution** run as isolated specialist processes, each with their own context and (optionally) their own LLM.
- The system supports **concurrency and isolation** between specialists — neither can pollute the other's context.
- The orchestrator routes work to specialists via **MCP** (standard "host → server" protocol).
- Specialists can call each other directly via **A2A** when the workflow demands it (e.g. a skill needs a tool mid-execution), without round-tripping through the orchestrator.
- The architecture is **extensible**: adding a new specialist later is a configuration change (a new Agent Card), not an orchestrator-code change.

## 2. Non-Goals

- Cross-machine deployment of specialists (A2A's transport allows it, but Day-1 is localhost only).
- Process pooling / horizontal scaling of a single specialist type.
- Interactive per-agent model wizard (Day-1: edit Card files manually).
- Migration of existing `.claude/` files (`.claude/memories/`, `.claude/credentials.json`, `.claude/settings.json`, `.claude/todos.json`, `.claude/agent.log`) — these stay where they are for backwards compatibility.
- Complex fault injection / chaos testing.

## 3. Architecture Overview

```
┌──────────────────────────── CLI process (orchestrator) ────────────────────────┐
│   REPL ──> Orchestrator (LangGraph StateGraph)                                 │
│              ├─ Permission Gate (three-tier pre-authorization)                 │
│              ├─ Agent Registry (reads .agent/agents/*.card.json)               │
│              ├─ Capability Router                                              │
│              ├─ MCP Host Client × N                                            │
│              └─ Unified Stream Multiplexer ([orchestrator]/[skill]/[tool] tags)│
└────────────┬───────────────────────────────────────────┬───────────────────────┘
             │ MCP / stdio                               │ MCP / stdio
             ▼                                           ▼
   ┌──────────────────────┐                ┌──────────────────────┐
   │  skill-agent (proc)  │ ◀── A2A/HTTP ──▶│  tool-agent  (proc)  │
   │  - SkillLoader       │  localhost:Px  │  - Tool Registry     │
   │  - own LLM           │                │  - own LLM (rare)    │
   │  - MCP Server (stdio)│                │  - MCP Server (stdio)│
   │  - A2A Server (HTTP) │                │  - A2A Server (HTTP) │
   └──────────────────────┘                └──────────────────────┘
```

**Process count (Day-1):** 1 orchestrator + 2 specialists = 3 processes total.

**Two protocols:**

- **MCP over stdio** for orchestrator ↔ specialist (subprocess pipe, zero network config).
- **A2A over HTTP localhost** for specialist ↔ specialist (each specialist binds an ephemeral port at startup).

## 4. Component Responsibilities

### 4.1 Orchestrator (CLI process)

- Sole owner of session conversation history.
- LangGraph `StateGraph` for routing decisions.
- Performs **pre-authorization** of every specialist call: signs a short-lived JWT (`authz_grant`) listing the exact allowed tools for this request.
- Manages specialist subprocesses: spawn, health-check, shutdown, Ctrl+C cancellation.
- Receives streamed output from specialists, prefixes each chunk with `[skill]` / `[tool]` / `[orchestrator]` based on `trace_id`, writes to terminal.

**Does NOT:** execute tools or skills directly; call LLM for the actual work (only for routing decisions).

### 4.2 skill-agent (subprocess, stateless)

- Scans `skills/*/SKILL.md` at startup, builds skill registry.
- Exposes each skill via MCP and A2A.
- On invocation: loads the SKILL.md, runs one LLM turn (using its own configured model) to execute the skill.
- If the skill needs a tool mid-execution, **calls tool-agent via A2A** (not back through orchestrator).

**Does NOT:** retain conversation history; execute tools itself; read session memory.

**Tool isolation convention:** the skill-agent process has the same Python import paths as the rest of the repo, so technically nothing prevents it from `import tool.file_ops`. The design forbids this: any tool side-effect MUST go through A2A to tool-agent, so that permission gating, audit logging, and the unified stream all see it. Enforced by convention + lint rule (`agents/skill_agent/` must not import from `tool/`).

### 4.3 tool-agent (subprocess, stateless)

- Loads `tool/*.py` registry at startup.
- Exposes each tool via MCP and A2A.
- On invocation: validates `authz_grant` (signature + expiry + allowed_tools check) → executes → returns result.
- Most tools do NOT call an LLM (read_file, grep, calculator, etc.); the per-agent model slot is reserved but rarely consumed.

**Does NOT:** make permission policy decisions (only verifies the orchestrator's grant); retain state.

## 5. Protocol Surface

### 5.1 MCP channel (orchestrator ↔ specialist)

JSON-RPC 2.0 over stdio. Standard MCP `initialize` → `tools/list` handshake at startup.

Invocation:

```jsonc
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "method": "tools/call",
  "params": {
    "name": "grep_search",
    "arguments": { "pattern": "TODO", "path": "src/" },
    "_meta": {
      "authz_grant": "<short-lived JWT>",
      "trace_id": "trace-abc",
      "agent_caller": "orchestrator"
    }
  }
}
```

Streaming via MCP `notifications/progress`; final result via normal response.

### 5.2 A2A channel (specialist ↔ specialist)

HTTP + JSON-RPC 2.0. Each specialist binds an ephemeral localhost port at startup; orchestrator distributes the port table to specialists via MCP notification after they all initialize.

Invocation (`POST localhost:Px/a2a`):

```jsonc
{
  "jsonrpc": "2.0",
  "id": "task-007",
  "method": "tasks/send",
  "params": {
    "task_id": "task-007",
    "skill_id": "tool.grep_search",
    "input": { "pattern": "TODO", "path": "src/" },
    "_meta": {
      "authz_grant": "<JWT, forwarded unchanged>",
      "trace_id": "trace-abc",
      "agent_caller": "skill-agent",
      "telemetry_sink": "stdio://orchestrator"
    }
  }
}
```

`telemetry_sink` makes the callee emit a telemetry event back to orchestrator over the existing MCP stdio reverse channel — orchestrator can observe and label peer-to-peer calls even though they bypass it.

### 5.3 `authz_grant` token

JWT signed with an HMAC key generated at session start. The orchestrator passes this same key to each specialist via env var at spawn time, so every specialist can verify signatures locally.

```jsonc
{
  "iss": "orchestrator",
  "sub": "skill-agent",
  "exp": <unix ts + 60s>,
  "permission_mode": "workspace-write",
  "allowed_tools": ["grep_search", "read_file"],
  "trace_id": "trace-abc"
}
```

**Verification (what specialists actually check):**

1. HMAC signature valid against the shared key.
2. `exp` not in the past.
3. The tool name being invoked appears in `allowed_tools`.

The `sub` field is **audit-only** — it records which specialist orchestrator originally entrusted. When skill-agent forwards the JWT via A2A to tool-agent, tool-agent still sees `sub: skill-agent` and that is correct: the JWT is a capability that can be delegated. Tool-agent logs the execution as "tool X invoked under grant originally issued to skill-agent (forwarded via A2A from skill-agent)". Authorization gating relies on `allowed_tools`, not on `sub`.

## 6. Agent Registry & Discovery

### 6.1 Agent Card

`.agent/agents/<id>.card.json`:

```jsonc
{
  "id": "skill-agent",
  "display_name": "Skill Specialist",
  "version": "1.0.0",
  "entrypoint": {
    "type": "python",
    "module": "agents.skill_agent.main",
    "args": []
  },
  "mcp": { "transport": "stdio" },
  "a2a": { "transport": "http", "port_strategy": "ephemeral" },
  "capabilities_hint": ["skill"],
  "model_override": null
}
```

Capabilities themselves are NOT written into the Card — they come from runtime `tools/list`. The Card describes how to bring the specialist up and which broad category it serves.

### 6.2 Startup sequence

1. Read all `.agent/agents/*.card.json`.
2. For each Card:
   - `subprocess.Popen` the entrypoint with stdin/stdout pipes and an env dict carrying the resolved API key.
   - Run MCP `initialize` → `tools/list`.
   - The specialist's init response includes the A2A port it bound.
   - Orchestrator records `(id, pid, mcp_client, a2a_url)` in an in-memory registry.
3. Orchestrator broadcasts the full `id → a2a_url` table to each specialist via MCP notification (so peers can find each other).
4. Enter REPL.

### 6.3 Capability Router

LangGraph node inside orchestrator. LLM decides which capability to call. Router looks up the owner in the merged capability index (`union of all specialists' tools/list`). Day-1 has no namespace collisions (skill names ≠ tool names); if conflicts ever arise, resolve by Card-level priority.

### 6.4 Runtime state

`.agent/runtime/state.json` (overwritten each launch, cleared on exit):

```jsonc
{
  "session_started": "2026-05-15T10:00:00Z",
  "orchestrator_pid": 12345,
  "specialists": [
    { "id": "skill-agent", "pid": 12346, "a2a_url": "http://127.0.0.1:51234" },
    { "id": "tool-agent",  "pid": 12347, "a2a_url": "http://127.0.0.1:51235" }
  ]
}
```

Used by `/agents` slash command and external diagnostic scripts.

## 7. Data Flow

### 7.1 Simple request (single specialist)

User: `读一下 README.md`

```
[orchestrator] route → tool.read_file
[orchestrator] permission_gate: workspace-write allows read_file → sign JWT
[orchestrator] MCP tools/call → tool-agent
[tool-agent] verify JWT → execute → stream chunks (MCP progress notifications)
[orchestrator] tag each chunk [tool], write to terminal
```

### 7.2 Complex request (A2A chain)

User: `用 ppt-master 把这份简历做成 PPT`

```
[orchestrator] route → skill.ppt-master, sign JWT with broad allowed_tools
[orchestrator] MCP → skill-agent
[skill-agent] load SKILL.md, plan steps via own LLM
[skill-agent] A2A → tool-agent (tasks/send, JWT forwarded, telemetry_sink set)
[tool-agent] verify JWT → emit telemetry event back to orchestrator → execute → return
[orchestrator] telemetry event surfaces as [tool] tag in the unified stream
... loop for each tool step ...
[skill-agent] MCP response with final result
[orchestrator] [skill] summary to terminal
```

Day-1: A2A calls from skill-agent are **serial**. Parallel A2A scheduling deferred.

### 7.3 Failure & cancellation

- Specialist exception → MCP error response → orchestrator surfaces the error and returns to REPL; specialist process stays alive.
- A2A call failure → callee returns HTTP error; caller decides retry/abort; ultimately surfaces via MCP to orchestrator.
- User Ctrl+C → orchestrator sends MCP `notifications/cancelled` to all specialists; in-flight A2A tasks are cancelled by the callee. Processes are NOT killed; next REPL turn reuses them.

## 8. Configuration Model

### 8.1 Precedence (low → high)

1. `config.py` PROVIDERS dict (built-in defaults).
2. `.claude/settings.json` (existing, unchanged — session-global model).
3. `.agent/agents/<id>.card.json` `model_override` (per-agent override).
4. Env var `LANGCHAIN_AGENT_MODEL__<AGENT_ID>` (temporary override, highest).

Day-1 default: all Card `model_override` fields are `null`; all three processes use the global model. The slot is structural, ready to be filled later.

### 8.2 Credential propagation

- Orchestrator reads `.claude/credentials.json` (existing behavior).
- For each specialist, resolves its model → identifies the `api_key_env` it needs → injects the value via the subprocess `env` dict at spawn time.
- Specialists see only a standard env var; they do not know `.claude/` exists.

### 8.3 New slash commands

- `/agents` — list specialists (id / pid / a2a_url / model / health).
- `/agents reload` — re-read Cards and restart specialists (dev convenience).

`/model` continues to set only the global model; per-agent overrides are file-edits (Card) for Day-1.

### 8.4 Mode switch

- `python cli.py` — multi-agent (new default).
- `python cli.py --single` — legacy single-agent path (no subprocesses spawned).

## 9. File & Code Layout

```
agent/
├── cli.py                       # thin dispatcher: --single → legacy.run_loop, else orchestrator.main
├── config.py                    # unchanged
├── project_context.py           # unchanged
│
├── orchestrator/                # NEW
│   ├── main.py
│   ├── graph.py                 # LangGraph StateGraph
│   ├── router.py
│   ├── permission_gate.py
│   ├── mcp_host.py
│   ├── registry.py
│   ├── stream_mux.py
│   └── telemetry.py
│
├── agents/                      # NEW
│   ├── shared/
│   │   ├── mcp_server.py
│   │   ├── a2a_server.py
│   │   └── authz.py
│   ├── skill_agent/
│   │   ├── main.py
│   │   ├── skill_executor.py
│   │   └── a2a_client.py
│   └── tool_agent/
│       ├── main.py
│       └── tool_executor.py
│
├── legacy/                      # NEW (existing single-agent loop extracted here)
│   └── single_agent_loop.py
│
├── tool/                        # unchanged (consumed by tool-agent)
├── skills/                      # unchanged (consumed by skill-agent)
│
├── .agent/                      # NEW runtime namespace
│   ├── agents/                  # Cards
│   ├── runtime/                 # state.json
│   └── logs/
│
├── .claude/                     # untouched; existing usage preserved
└── tests/
    ├── test_orchestrator/
    ├── test_skill_agent/
    ├── test_tool_agent/
    └── test_e2e_multi_agent/
```

Touched existing files:

| File | Change |
|---|---|
| `cli.py` | Add `argparse --single`; dispatch to `orchestrator.main` or `legacy.single_agent_loop`. |
| `tools.py` | Unchanged; tool-agent reuses the existing registry. |
| `skill_loader.py` | Unchanged; skill-agent reuses. |
| `tool_permissions.py` | Move policy decision logic into `orchestrator/permission_gate.py`; keep mode definitions here. |

New dependencies: `mcp` (official Python SDK), `a2a-sdk` (official), `pyjwt`. (`langgraph` already in use.)

## 10. Testing Strategy

### 10.1 Pyramid

```
       /\        E2E       3–5 cases (subprocess + mock LLM)
      /  \
     /----\     Integration ~15 cases (subprocess + mock LLM)
    /      \
   /--------\
  /          \  Unit       ~80% coverage (pure functions, ms)
 /____________\
```

### 10.2 Unit (highlights)

- `permission_gate`: per-mode allow/deny matrix; JWT field correctness.
- `router`: capability resolution, priority ordering, unknown-capability error.
- `stream_mux`: concurrent traces don't cross-contaminate labels.
- `authz`: valid / expired / tampered / wrong-sub JWTs.
- `mcp_server` skeleton: handles `initialize` / `tools/list` / `tools/call`.

### 10.3 Integration (real subprocess spawn)

- `test_spawn_and_handshake`
- `test_mcp_call_roundtrip`
- `test_a2a_peer_call` (also validates telemetry surfaces to orchestrator)
- `test_authz_violation` (tampered JWT, off-whitelist tool)
- `test_ctrl_c_cancel` (specialist receives cancellation, stays alive)
- `test_specialist_crash_recovery` (kill -9 a specialist; orchestrator errors gracefully, doesn't crash)

### 10.4 E2E

- `test_e2e_simple_tool`: `python cli.py prompt "读 README"` → exit 0, stdout contains `[tool]` tag and file content.
- `test_e2e_skill_a2a_chain`: `python cli.py prompt "用 ppt-master ..."` → label order matches §7.2.
- `test_e2e_legacy_mode`: `python cli.py --single prompt "..."` → no subprocesses spawned (verified via `psutil`).

### 10.5 Infrastructure

- A2A port via `port=0` (OS-assigned); tests never assume specific ports.
- A new **`mock` provider** is added to `config.py`'s `PROVIDERS` dict, returning deterministic stub responses. Tests select it via `LANGCHAIN_AGENT_MODEL=mock` so specialist subprocesses run without hitting any real LLM API. This addition is in-scope for Day-1 — the integration and E2E tests depend on it.
- Trace-assertion helper parses unified-stream output by `[tag]` for ordered assertions.

CI runs all layers; `pytest -m "not e2e"` for fast local dev.

## 11. Open Questions / Future Work

Deferred from Day-1 scope:

- Parallel A2A scheduling inside a single skill (Day-1 is serial).
- Interactive per-agent model wizard (Day-1: edit Card files).
- Process pooling (Day-1: one specialist instance per type).
- Cross-machine A2A (already supported by protocol; only localhost in Day-1).
- Complex chaos / fault injection tests.
- Migration of existing `.claude/` files into `.agent/` (explicitly out of scope).
