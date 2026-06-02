# Phase B: warm-pool + persistent turn-loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Status:** Implemented 2026-06-02 (Tasks 1–9 complete). This doc reflects what was built, including two refinements discovered during execution (noted inline).

**Goal:** Remove the ~7s specialist cold-start from most web turns by reusing bootstrapped `MCPHost`s across turns, behind a reversible `WEB_POOL_ENABLED` flag (default off = today's per-turn cold spawn).

**Architecture:** A process-wide `SpecialistPool` caches fully-bootstrapped `(host, router, hmac_key)` triples keyed by the spawn-env *signature* `(user_id, workspace_root, model_id, base_url, api_key, protocol)`. Because an `MCPHost` holds asyncio stdio connections bound to the event loop it was created on, the pool and every turn that touches a pooled host live on **one persistent "turn loop" thread** (replacing today's fresh-thread-per-turn). The serving loop submits a turn coroutine to that loop via `run_coroutine_threadsafe` and streams events back over the existing thread-safe queue. Pooled hosts bake their `hmac_key` at creation, so a turn that leases a host dispatches with *that host's* key, not a fresh per-turn one.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, pytest / pytest-asyncio. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-01-multi-user-concurrency-turncontext-design.md` (Phase B, lines 144–180).

**Scope:** Phase B only — the web surface. CLI and gateway are unchanged. Phases C (security gate) and D (heuristics) are separate plans.

**Builds on Phase A:** `orchestrator/turn_context.py` (`TurnContext`, `turn_env()`), `MCPHost(*, hmac_key, turn_env=None)`, and the `_TURN_SEMAPHORE` / `WEB_MAX_CONCURRENCY` guard.

---

## Key design facts

- **Pool key = spawn signature**, NOT the whole `turn_env`. Excluded: `permission_mode` (web is always `WEB_PERMISSION_MODE`; authz rides per-call in the hmac grant), `runtime_dir` (per pooled host), `hmac_key` (per pooled host; the turn reuses the host's key).
- **Loop affinity is load-bearing.** A pooled `MCPHost`'s stdio transports are bound to the loop that created them. The pool is created on, and only ever driven from, the persistent turn loop.
- **Reversibility.** `WEB_POOL_ENABLED=0` (default) keeps today's behavior: each turn cold-spawns a private host and shuts it down. **Refinement during execution:** the disabled path *bypasses the pool entirely* (cold-spawn a private `MCPHost` + `shutdown_all` in the finally) rather than routing a one-shot host through `acquire`/`release` — simpler and byte-for-byte today's behavior.

---

## File Structure

**Create:** `orchestrator/specialist_pool.py`, `web/turn_loop.py`, `tests/test_orchestrator/test_specialist_pool.py`, `tests/test_web/test_turn_loop.py`.

**Modify:** `web/config.py` (pool knobs), `web/bridge.py` (lease + turn-loop submission + warm-up seed + sweeper), `web/app.py` (shutdown drain + sweeper start/stop).

---

## Task 1: `pool_signature` + `Lease` + cold-spawn `acquire` / pooling `release`

**Files:** Create `orchestrator/specialist_pool.py`; Test `tests/test_orchestrator/test_specialist_pool.py`.

- [x] Step 1: failing test — `pool_signature` excludes permission/runtime/hmac; `acquire` cold-spawns then `release` pools for reuse (reused host => reused baked hmac_key); different signature spawns a separate host.
- [x] Step 2: run → FAIL (`ModuleNotFoundError`).
- [x] Step 3: implement `pool_signature(ctx)`, `Lease`, `_Entry`, and `SpecialistPool` with `acquire` (idle-match → reuse; else reserve a slot under the lock, cold-spawn the `(host, router)` outside the lock via the injected `factory`, assign a per-host `hmac_key` + `runtime_dir`), `release` (mark idle, stamp `last_used`, `notify`), `sweep`, `drain`, `_evict_one_idle` (LRU), `_shutdown`. Uses an `asyncio.Lock` + `asyncio.Condition` for cap/queue coordination.
- [x] Step 4: run → PASS.
- [x] Step 5: commit `feat(pool): SpecialistPool — signature-keyed host reuse with cold-spawn acquire/release`.

## Task 2: cap + LRU eviction + queue-at-cap

- [x] Tests: LRU evicts the oldest idle host when over cap (deterministic clock); `acquire` blocks until a `release` frees a slot when all hosts are leased at cap. (Behavior shipped in Task 1; these lock it.)
- [x] Run → PASS; commit `test(pool): lock cap/LRU-eviction/queue-at-cap behavior`.

## Task 3: idle-TTL sweep + drain

- [x] Tests: `sweep()` shuts down idle hosts past the TTL only (leased + fresh-idle kept); `drain()` shuts down all hosts (idle AND leased).
- [x] Run → PASS; commit `test(pool): lock idle-TTL sweep + full drain`.

## Task 4: `TurnLoop` — the persistent event-loop thread

**Files:** Create `web/turn_loop.py`; Test `tests/test_web/test_turn_loop.py`.

- [x] Step 1: failing test — `run_coroutine`/`run_coroutine_factory` execute on the loop thread (≠ serving loop); `stop()` is idempotent.
- [x] Step 2: run → FAIL.
- [x] Step 3: implement `TurnLoop` (daemon thread runs `new_event_loop().run_forever()`; `run_coroutine`, `run_coroutine_factory`, `call_soon`, `stop`, `is_running`, `loop_id`).
- [x] Step 4: run → PASS; Step 5: commit `feat(web): TurnLoop — one persistent event-loop thread for pooled hosts`.

**Refinement (Task 7 surfaced it):** `start()` must be restart-safe — clear `_ready` and `_loop` before spawning the new thread, else a restart returns on the already-set event while `_loop` still points at the previous, closed loop. Added a `test_restart_after_stop_runs_on_a_fresh_loop` regression (assert work runs on the new running loop; do NOT compare `loop_id` to the old one — `id()` is reused after the old loop is freed).

## Task 5: config knobs

**Files:** Modify `web/config.py`; Test `tests/test_web/test_config.py`.

- [x] failing test → add `pool_enabled()` (default False), `pool_max_hosts()` (default 8), `pool_idle_ttl()` (default 600.0), all env-driven with fallback → PASS → commit `feat(web): WEB_POOL_ENABLED / WEB_POOL_MAX_HOSTS / WEB_POOL_IDLE_TTL knobs`.

## Task 6: wire `web/bridge.py` to the pool + turn loop

**Files:** Modify `web/bridge.py`; Tests `tests/test_web/test_bridge.py`, `test_concurrency_isolation.py`.

- [x] Step 1: module-level `_TURN_LOOP = TurnLoop()`, lazy `_POOL` (`_POOL_LOCK`), `_ensure_turn_loop()`, `_host_factory(signature, runtime_dir, hmac_key)` (rebuilds a throwaway `TurnContext` → `MCPHost(turn_env=...)` + `_bootstrap`), `_get_pool()`.
- [x] Step 2: `_run_streaming_locked` — `ensure_specialists` leases from the pool when `config.pool_enabled()` (sets `dispatch_hmac_key = lease.hmac_key`) **else** cold-spawns a private host (disabled path bypasses the pool). `finally`: `release(lease)` (pooled) and/or `host.shutdown_all()` (disabled). Dispatch hmac is late-bound via `hmac_key=lambda: dispatch_hmac_key`; `_plan_and_dispatch` resolves `hmac_key() if callable(...)` after `ensure_specialists()`.
- [x] Step 3: `_stream_off_loop` submits `_produce` to the shared `TurnLoop` via `run_coroutine_factory` (no more per-turn `threading.Thread`/`asyncio.run`); the `finally` cancels the turn on the loop on client disconnect (`future.cancel()`) and blocks on `await asyncio.to_thread(future.result)` so the turn fully unwinds (lease release / cleanup) before the semaphore slot frees.
- [x] Step 4: run bridge + concurrency suites → PASS (also verified the pool-ON capability/dispatch tests).
- [x] Step 5: commit `feat(web): lease specialist hosts from the pool on a persistent turn loop`.

## Task 7: warm-up seeds a pooled host; app shutdown drains the pool

**Files:** Modify `web/bridge.py` (`warm_capability_catalog`), `web/app.py`; Tests `test_bridge.py`, `test_app.py`.

- [x] warm-up: after building the catalog, when `pool_enabled()`, acquire+release one pooled host for the default signature on the turn loop (`_seed`). Test patches `_get_pool` + `_capability_catalog` and asserts one acquire.
- [x] app shutdown hook `_drain_pool`: drain the pool on the turn loop, then `_TURN_LOOP.stop()`. Test uses a `TestClient` with the real bridge (warm-up patched to a no-op to stay hermetic) and a fake pool, asserts `drain` called once.
- [x] Drains via `await asyncio.to_thread(fut.result)` (not `wrap_future`) so the lifespan-task await stays loop-local and doesn't entangle anyio's lifespan cancel scope.
- [x] commit `feat(web): warm-up seeds a pooled host; shutdown drains the pool + stops turn loop` (also carries the Task-4 restart fix).

## Task 8: idle-TTL sweeper wired into the lifecycle

**Files:** Modify `web/bridge.py` (`_pool_sweeper`), `web/app.py`; Test `test_bridge.py`.

- [x] failing test → `_pool_sweeper(interval)` loops `await asyncio.sleep(interval)` then `_get_pool().sweep()` (best-effort) → PASS.
- [x] app startup starts the sweeper on the turn loop when `pool_enabled()`; shutdown cancels it before draining.
- [x] commit `feat(web): periodic idle-TTL pool sweeper on the turn loop`.

## Task 9: full-suite regression gate + warm-path smoke

- [x] **Step 1 (authoritative gate):** `python -m pytest -q` with pool default OFF → **800 passed, 1 skipped**, clean. Behavior-stable vs Phase A (cold-spawn + teardown per turn; only the execution thread changed to the shared loop).
- [x] **Step 2 (pool-on validation, TARGETED):** `WEB_POOL_ENABLED=1 python -m pytest tests/test_orchestrator/test_specialist_pool.py tests/test_web/test_turn_loop.py tests/test_web/test_bridge.py tests/test_web/test_app.py tests/test_web/test_config.py -q` → PASS, clean. Exercises lease-reuse, the persistent loop, warm-up seeding, the shutdown drain.

  > Do NOT run the *entire* `tests/test_web/` under a global `WEB_POOL_ENABLED=1`. Several legacy bridge tests patch the old inline-spawn path and aren't pool-aware, so under global pool-on they route through the real `_host_factory` and spawn real specialist subprocesses; combined with `test_concurrency_isolation`'s `importlib.reload(bridge)` (which orphans the module's `_POOL`/`_TURN_LOOP`), a real host is stranded on a stopped loop and the daemon thread is killed at interpreter exit, printing harmless asyncio/anyio `GeneratorExit` teardown noise. All assertions still pass; it's a test-harness artifact (no module reload, single loop, drained at shutdown in production).

- [ ] **Step 3 (manual smoke, needs a provider key):** `WEB_POOL_ENABLED=1 WEB_MAX_CONCURRENCY=4 WEB_AUTH_SECRET=dev WEB_SIGNUP_CODE=dev python -m web --host 127.0.0.1` — two capability turns from one account: first pays ~7s, second is hot. Confirm clean exit (no orphaned specialists) on Ctrl-C.

---

## Notes for the implementer

- **Reversibility is the contract.** With `WEB_POOL_ENABLED` unset, behavior is identical to Phase A; risky parts (host reuse, persistent loop) only change behavior when the flag is on.
- **Loop affinity is the #1 regression risk.** Anything that creates/drives an `MCPHost` must do so on the `TurnLoop`. Never `await pool.acquire(...)` from the serving loop.
- **hmac_key correctness.** A pooled host's specialists were spawned with the host's baked key; always dispatch with `lease.hmac_key`.
- **Out of scope:** CLI and gateway keep their per-turn host lifecycle.
