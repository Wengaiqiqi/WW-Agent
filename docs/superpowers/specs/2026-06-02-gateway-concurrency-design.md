# Gateway true concurrency via `GATEWAY_MAX_CONCURRENCY` — Design

**Date:** 2026-06-02
**Status:** Approved
**Scope:** The feishu/qq gateway turn path only. Web and CLI unchanged.

## Problem

The gateway serializes every turn across all users via two locks:
- `gateway.runner._CONCURRENCY_GUARD` (`asyncio.Lock`) — covers in-loop async-native callers (QQ, the REPL-as-background-task path).
- `gateway.feishu_ws._dispatch_lock` (`threading.Lock`) — covers the feishu WS adapter, which runs each inbound message on its own SDK worker thread via `asyncio.run` (so an asyncio lock alone wouldn't span them).

Serialization was a deliberate choice ("single queue is fine for chat-bot throughput"), but for a multi-user bot it means user B waits behind user A's entire turn (LLM round-trip + any specialist cold-spawn). 

The blocker to concurrency is not just the locks: `_run_turn_locked` wraps the whole turn in `scoped_env(ctx.turn_env())`, which **mutates process-global `os.environ`**. Two concurrent turns would clobber each other's planner config and memory user. Phase A already gave the gateway a `TurnContext`, per-`turn_id` runtime dirs, and the subprocess env via `MCPHost(turn_env=...)`; the remaining global state is this in-process env mutation.

## Goals

- Multi-user concurrency on the gateway, gated by `GATEWAY_MAX_CONCURRENCY` (default 1 = identical to today; reversible).
- Eliminate the per-turn global `os.environ` mutation so concurrent turns are isolated.
- CLI and REPL single-user behavior unchanged.

## Non-goals

- No warm pool for the gateway (turns keep per-turn cold spawn — concurrent, but each capability turn still pays its own ~7s spawn). The web `SpecialistPool` is not wired in.
- No change to the feishu "thread-per-message" model (only its lock changes to a bounded semaphore).
- No change to the A2A/MCP wire protocol.

## Design

### 1. Eliminate the per-turn env mutation (correctness)

Thread the context into the only two in-process readers of per-turn env, then delete the `scoped_env(ctx.turn_env())` wrapper in `_run_turn_locked`:

- **Planner LLM config.** `orchestrator.main._build_orchestrator_llm(cfg=None)` — use `cfg` when given, else `load_active_config()` (CLI/legacy unchanged). `gateway.runner._build_planner(router, *, context_text="", cfg=None)` forwards `cfg`. `_run_turn_locked` resolves `cfg = resolve_config(ctx)` and passes it down. (`get_api_key` already reads `cfg.api_key` after Phase A, so the custom-endpoint key travels on `cfg`.)
- **Memory snapshot.** `tool.tool_memory.snapshot_for_system_prompt(user=None)` — use `user` when given, else the `LANGCHAIN_AGENT_MEMORY_USER` env (CLI/legacy unchanged). `gateway.runner._build_planner_context(session_key, *, memory_user="")` passes `ctx.user_id`.

The subprocess channel is unaffected — specialists still get the per-turn env via `MCPHost(turn_env=ctx.turn_env())`. After this, a gateway turn reads no per-turn process-global env in-process, so removing `scoped_env` is safe.

**Audit obligation:** before deleting `scoped_env`, grep the in-process gateway/orchestrator turn path for any other `os.environ` read of a `LANGCHAIN_AGENT_*` per-turn var; thread any found through `ctx` too. (Expected: only the two above.)

### 2. Concurrency flag + bounded semaphores

- New `gateway.runner.max_concurrency()` reads `GATEWAY_MAX_CONCURRENCY` (default 1; non-int/blank → 1; floor 1). Mirrors `web.config.max_concurrency()`.
- `_CONCURRENCY_GUARD` (`asyncio.Lock`) → `_GATEWAY_SEMAPHORE = asyncio.Semaphore(max_concurrency())`; `run_turn` does `async with _GATEWAY_SEMAPHORE`.
- `feishu_ws._dispatch_lock` (`threading.Lock`) → `_dispatch_sem = threading.BoundedSemaphore(runner.max_concurrency())`; `with _dispatch_sem:`.

At default 1, a semaphore of 1 is behaviorally identical to the lock — zero behavior change. The two semaphores are independent, but only one caller model is active per deployment (feishu WS *or* QQ *or* REPL), so the active one bounds total concurrency.

### Data flow (unchanged shape, env removed)

```
inbound msg ─► run_turn (acquire semaphore) ─► _run_turn_locked
                                                   │ ctx = TurnContext.from_env(...) (+ user_id, ws-write, per-turn-id runtime dir)
                                                   │ cfg = resolve_config(ctx)
                                                   ├─ _build_planner_context(session_key, memory_user=ctx.user_id)
                                                   ├─ _build_planner(router, context_text, cfg=cfg)
                                                   ├─ host = MCPHost(turn_env=ctx.turn_env())   # subprocess channel
                                                   └─ dispatch ... ; finally: shutdown + rmtree(ctx.runtime_dir)
                                            (no scoped_env / no os.environ mutation)
```

### Error handling

Unchanged: the per-turn `finally` (host shutdown, runtime-dir cleanup, session append) already operates on turn-local state. Removing `scoped_env` removes a `finally`-based env restore that is no longer needed.

## Testing

- **Default serialized:** `max_concurrency()` returns 1 with no env; semaphore of 1 preserves today's behavior. Existing `tests/test_gateway/` suite is the parity guard.
- **Env isolation:** building a planner/cfg from two different contexts yields two configs and does not touch `os.environ` (mirrors the web isolation test).
- **Concurrency:** two `run_turn` calls with different `user_id`/`model_id` fired in parallel (with `GATEWAY_MAX_CONCURRENCY=2`, fake planner/host) each see their own config + memory user; with default 1 they serialize.
- **Semaphore bound:** N+1 concurrent turns at limit N — the (N+1)th waits.
- **Full suite** stays green (the gateway e2e path spawns real specialists).

## Rollout

`GATEWAY_MAX_CONCURRENCY` ships at 1 (identical to today). Validate in production, then raise it:

```
GATEWAY_MAX_CONCURRENCY=5 python -m gateway ...
```

Unset or `=1` → today's serialized behavior. Reversible without a revert.

## Risks

- **Missed in-process env reader** — mitigated by the audit obligation in §1; the default-1 flag means any miss is harmless until the flag is raised.
- **feishu thread-per-message under load** — N concurrent threads each run `asyncio.run` + cold-spawn specialists; bounded by the semaphore. Per-turn-id runtime dirs (Phase A) prevent `.agent/runtime` collisions.
