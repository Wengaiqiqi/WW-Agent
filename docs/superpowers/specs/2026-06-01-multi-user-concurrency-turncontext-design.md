# Major optimization: multi-user concurrency via explicit `TurnContext`

**Date:** 2026-06-01
**Status:** Design approved, pending spec review
**Scope:** One spec, four phased workstreams (A foundation → B → C → D)

## Problem

Almost every hard problem in this codebase traces to one root: **the orchestrator
configures itself from process-global `os.environ`, mutated per turn, serialized
by a single `_CONCURRENCY_GUARD`.** This worked for the original single-user CLI
but the gateway and web surfaces bolted multi-user/multi-tenant onto it.

Consequences observed during review:

- **Concurrency ceiling.** The web server processes exactly one turn at a time
  across *all* users (`_CONCURRENCY_GUARD` held for the whole turn). Combined with
  ~7s specialist cold-start, throughput is very low.
- **Isolation is fragile.** Per-turn config (`LANGCHAIN_AGENT_MEMORY_USER`,
  `WORKSPACE_ROOT`, `MODEL`, `BASE_URL`, `API_KEY`, `PROTOCOL`, `RUNTIME_DIR`) is
  written onto the shared `os.environ` and restored in a `finally`. Two copies of
  this snapshot/restore logic (gateway vs web) had already begun to drift.
- **warm-pool is blocked.** Specialists bake their LLM/memory/workspace from env
  at spawn, so a naively reused process would serve the wrong user/model — the
  per-turn-global-env model makes safe pooling impossible.

`os.environ` is not only in-process state; it is also the **configuration channel
to specialist subprocesses**, which read env at spawn. Any fix must address both
the in-process threading *and* the cross-process channel.

## Goals

- Multi-user **true concurrency** on the web surface (~5–10 concurrent turns).
- Eliminate process-global per-turn state; make per-turn config explicit.
- Enable a bounded warm-pool that removes the ~7s cold-start on most turns.
- Harden the security posture into a CI gate so refactors can't reopen holes.
- Keep all three entry surfaces (CLI, feishu/qq gateway, web) **behavior-stable**.

## Non-goals (explicit)

- No change to the A2A/MCP wire protocol.
- No remote/distributed specialist pool — single-process pool only.
- No CLI/gateway UX or config changes.
- Heuristics (`fast_route`, JSON-echo) are **hardened, not restructured**.

## Inputs locked during brainstorming

| Decision | Value |
|----------|-------|
| Spec scope | All four workstreams in one phased spec |
| Concurrency target | Multi-user **true** parallelism, ~5–10 ceiling |
| Approach | Explicit `TurnContext`; subprocess env built per-turn; no `os.environ` mutation |
| warm-pool sizing | Bounded, keyed by `(user, endpoint-signature)`, LRU + idle TTL |
| Security gate | CI security test set + startup self-check + static scan |
| Heuristics | Keep + harden; no structural change |
| Compat | All three surfaces behavior-stable; 776 tests as guardrail |

---

## Phase A — the `TurnContext` foundation

### New unit: `orchestrator/turn_context.py`

A single frozen dataclass carrying the per-turn state that today lives in env:

```python
@dataclass(frozen=True)
class TurnContext:
    turn_id: str            # uuid — keys the runtime dir, the pool, tracing
    user_id: str            # memory scope
    workspace_root: Path
    permission_mode: str
    model_id: str           # was LANGCHAIN_AGENT_MODEL
    base_url: str           # was LANGCHAIN_AGENT_BASE_URL
    api_key: str            # was LANGCHAIN_AGENT_API_KEY
    protocol: str           # was LANGCHAIN_AGENT_PROTOCOL
    session_key: str        # history lookup
    trace_id: str
    hmac_key: str

    @classmethod
    def from_env(cls) -> "TurnContext": ...   # reproduces today's env-based behavior
    def turn_env(self) -> dict[str, str]: ...  # the env dict passed to spawned subprocesses
```

### Three seams change; the rest is mechanical

1. **Entry points are the only place env/request is read.** CLI builds the context
   from env + settings.json (`from_env`); the gateway builds it from the message;
   the web route builds it from the request row. The current `_web_turn_env` /
   `scoped_env` **export-to-`os.environ`** logic is *deleted* — context is built,
   not exported.

2. **In-process config resolution takes the context, not env.**
   - `config._settings.resolve_config(ctx)` (pure): applies ctx overrides onto the
     settings.json base, including the protocol whitelist already added.
   - `build_llm(cfg)` and `get_api_key(cfg)` already take `cfg`. `get_api_key`
     **stops reading the `LANGCHAIN_AGENT_API_KEY` env override** and reads
     `cfg.api_key` (sourced from the context).
   - `load_active_config()` remains as a thin `resolve_config(TurnContext.from_env())`
     shim so the CLI path is unchanged.

3. **Subprocess spawn gets an explicit env dict.** `MCPHost.spawn` already filters
   the child env to a passthrough whitelist; it gains a `turn_env: dict` param
   built from the context (`ctx.turn_env()`), merged into the child `env=`. **The
   parent `os.environ` is never mutated**, so parallel spawns can't see each
   other's config.

### Concurrency

With no shared mutable env and per-`turn_id` runtime dirs (replacing the per-PID
dir, which collides for parallel turns in one process), `_CONCURRENCY_GUARD` is
removed. Each turn owns its host, runtime dir, and config end to end.

### Data flow (unified across surfaces)

```
entry point ─► build TurnContext ─► resolve_config(ctx) ─► build planner
     │                                                          │
     │                          ┌──────── prose? stream tokens, done
     ▼                          │
 dispatch(ctx, decision) ───────┤── capability? get host from pool/spawn(ctx)
                                │                     │
                                └──────────────► run graph / A2A delegate
                                          spawn env = passthrough + ctx.turn_env()
```

### Web execution model under real concurrency

Today `run_turn_streaming` holds `_CONCURRENCY_GUARD` for the whole turn and runs
it on a raw worker thread (`_stream_off_loop`). The guard is removed so N turns run
concurrently. Isolation now comes from the context: each turn has its own
`turn_id` runtime dir, its own host (or a pooled one in Phase B), and its config
travels in the spawn `env=` dict, not `os.environ`. The remaining shared state is
read-only (settings.json, the static catalog cache) or already concurrency-safe
(`web/store.py`, now WAL: one writer + many short writes, no shared connection).

Error/cancel semantics are preserved — the per-turn `finally` (host shutdown,
runtime-dir cleanup, client-disconnect cancellation) operates on turn-local state
instead of process-global.

---

## Phase B — the warm-pool

### Pool key = the spawn-env signature

A specialist bakes its LLM + memory + workspace at spawn, so two turns can share a
warm host **iff** these match: `(user_id, workspace_root, model_id, base_url,
api_key, protocol)`. `permission_mode` is **not** in the key — it rides per-call in
the authz grant, so one warm tool-agent serves both read-only and workspace-write
turns.

### `SpecialistPool` (new, process-wide)

- `acquire(ctx)` → an idle host matching the signature, else cold-spawn one (the
  only ~7s path remaining).
- `release(host)` → return to the idle set (not shut down).
- A leased host is **exclusive to one turn** (specialists aren't built for
  concurrent A2A into the same process).
- Bounds: global cap on live hosts (config, default ~8); LRU-evict (shutdown) the
  oldest idle host when over cap; a background sweeper shuts hosts idle past a TTL.
  Server shutdown drains the pool.

### Load-bearing constraint — event-loop affinity

An `MCPHost` holds asyncio stdio connections bound to the loop it was created on,
so a pooled host can't be driven from a different thread's loop. Today's
"fresh worker thread per turn" makes pooling impossible. **Phase B replaces it with
one persistent "turn loop" thread that owns the pool and runs turn coroutines
concurrently;** the blocking bits (planner `.invoke`, cold spawn) are offloaded with
`asyncio.to_thread` so the loop stays responsive. The serving loop submits turns via
`run_coroutine_threadsafe` and streams events back over the existing queue. This is
the natural home for the pool and also simplifies the threading.

### Warm-up

The startup `warm_capability_catalog` (which today spawns the fleet only to read
schemas and discard it) instead seeds **one real pooled host** for the default
signature — same cost, but the first capability turn is now warm.

---

## Phase C — security gate

Independent of A/B. Three parts:

- **`tests/test_security/` regression suite** — consolidate the now-scattered
  security assertions and fill gaps, each an explicit named test: SSRF base_url
  rejection (private/loopback/metadata), auth required on protected routes,
  signup-gate enforced, JWT secret persistence/stability, TLS verify stays on with
  a pin, non-loopback bind refused without secrets, API keys stored encrypted
  (ciphertext in DB, plaintext only in memory). This is the "did a refactor reopen
  a hole?" tripwire — especially important because Phase A/B touch the config and
  spawn paths.
- **Startup self-check** — `web.config.assert_safe_for_exposure()` called at server
  start: refuses (exits non-zero) a network bind without `WEB_AUTH_SECRET` +
  `WEB_SIGNUP_CODE` (extends the `web.__main__` guard already added), warns on weak
  settings. One place that encodes "safe to expose."
- **Static scan CI job** — add a `bandit` job to `.github/workflows/ci.yml` with a
  checked-in baseline so it gates *new* findings without drowning in existing
  noise; optionally `pip-audit` for dependency CVEs.

---

## Phase D — heuristics (harden only)

No structural change.

- `fast_route`: keep the current shape; the over-broad `startswith` is already
  removed and unit-tested; the `LANGCHAIN_AGENT_DISABLE_FAST_ROUTE` escape hatch
  stays. Add further whole-word/boundary guards only if review surfaces them.
- `agent_loop` JSON-echo: keep the narrowed "withhold only after a tool result" +
  trailing-char object extraction already landed. No rewrite. Lock behavior with
  the regression tests already added.

---

## Testing strategy

**Guardrail:** the 776-test suite stays green at every merge. The only existing
tests intentionally changed are those asserting on the deleted env-mutation
(`_web_turn_env`/`scoped_env` exporting to `os.environ`) — rewritten to assert on
the `TurnContext` / spawn-env dict.

**New tests by phase:**

- **A:** `TurnContext.from_env()` round-trip; `resolve_config(ctx)` (ctx overrides
  beat settings.json; protocol whitelist); **isolation test** — two contexts
  produce two spawn-env dicts and `os.environ` is untouched; **concurrency test** —
  two web turns fired in parallel with different users + endpoints each see their
  own config (extends the existing fake-bridge pattern).
- **B:** pool `acquire`/`release`, signature match/mismatch, LRU eviction at cap,
  idle-TTL sweep, exclusive lease, loop-affinity (pooled host reused across two
  sequential turns on the persistent loop), warm-up seeds one pooled host.
- **C/D:** the security suite and the heuristics regression tests above.

## Phasing — four independently mergeable PRs, A first

1. **A1** — `TurnContext` + `resolve_config` + route config through `cfg`. No
   behavior change (`from_env` shim).
2. **A2** — spawn-env dict + per-`turn_id` runtime dir + **remove
   `_CONCURRENCY_GUARD`** (parallelism turns on here). Gated behind
   `WEB_MAX_CONCURRENCY` (default 1 → identical to today; raise to enable).
3. **B** — persistent turn-loop + `SpecialistPool` + warm-up (falls back to
   per-turn cold spawn if disabled).
4. **C** and **D** — anytime, independent.

## Rollout safety

`WEB_MAX_CONCURRENCY` ships the plumbing at concurrency=1 (identical to today),
lets us validate in production, then raise it. The pool (B) can fall back to
per-turn cold spawn. Both are reversible without a revert.

## Risks

- **Event-loop affinity (B)** is the subtlest part; the persistent-turn-loop change
  is where regressions are most likely. Covered by loop-affinity + concurrency
  tests and gated by the pool-disable fallback.
- **Mechanical breadth (A)** touches the config layer, `mcp_host`, `runner`,
  `bridge`, `repl_controller`, `turns`. Low individual risk, high count — the
  776-test suite is the guardrail and A1 is a no-behavior-change shim first.
- **Specialist concurrency** — leased hosts are exclusive per turn to avoid
  concurrent A2A into one subprocess; the cap + queue keep process count bounded.
