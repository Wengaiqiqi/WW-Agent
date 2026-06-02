# Gateway true concurrency (`GATEWAY_MAX_CONCURRENCY`) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let multiple users' gateway turns run concurrently, gated by `GATEWAY_MAX_CONCURRENCY` (default 1 = identical to today; reversible).

**Architecture:** Remove the per-turn global `os.environ` mutation (thread `ctx` into the planner config + memory snapshot, then delete `scoped_env`), and replace the two serializing locks with bounded semaphores sized from `GATEWAY_MAX_CONCURRENCY`.

**Spec:** `docs/superpowers/specs/2026-06-02-gateway-concurrency-design.md`

---

## Task 1: thread `cfg`/`user` into the two env readers (no behavior change yet)

**Files:** `orchestrator/main.py`, `tool/tool_memory.py`; Tests: `tests/test_orchestrator/`, existing memory tests.

- [ ] **Step 1: failing tests**

```python
# tests/test_orchestrator/test_gateway_env_threading.py
from __future__ import annotations


def test_build_orchestrator_llm_accepts_explicit_cfg(monkeypatch):
    from config import make_config
    from orchestrator.main import _build_orchestrator_llm
    cfg = make_config("mock")  # a provider that build_llm handles without network
    llm = _build_orchestrator_llm(cfg)
    assert llm is not None  # built from the passed cfg, no load_active_config()


def test_snapshot_for_system_prompt_accepts_explicit_user(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
    from tool import tool_memory
    # Explicit user must not require the env var to be set.
    out = tool_memory.snapshot_for_system_prompt(user="alice")
    assert isinstance(out, str)  # no crash; scoped to alice's dir
```

- [ ] **Step 2: run → FAIL** (`_build_orchestrator_llm` takes no arg; `snapshot_for_system_prompt` takes no `user`).

Run: `python -m pytest tests/test_orchestrator/test_gateway_env_threading.py -v`

- [ ] **Step 3: implement**

`orchestrator/main.py`:
```python
def _build_orchestrator_llm(cfg=None):
    """Build a chat model for the orchestrator's planner. ``cfg`` (a resolved
    ActiveConfig, e.g. from resolve_config(ctx)) lets a caller avoid the
    process-global load_active_config() env read; None keeps the legacy path."""
    from config import build_llm, hydrate_env_from_credentials, load_active_config
    hydrate_env_from_credentials()
    return build_llm(cfg or load_active_config())
```

`tool/tool_memory.py` — in `snapshot_for_system_prompt`, accept an explicit user and prefer it over the env var. Find the function and change its signature to `def snapshot_for_system_prompt(user: str | None = None) -> str:`; where it currently reads `os.environ.get(_USER_ENV_VAR, ...)` to pick the user dir, use `user if user is not None else os.environ.get(_USER_ENV_VAR, "")`. (Grep `_USER_ENV_VAR` / `def snapshot_for_system_prompt` in `tool/tool_memory.py`; thread the param to wherever the user scope is resolved.)

- [ ] **Step 4: run → PASS**; then the broader suites:

Run: `python -m pytest tests/test_orchestrator/ -q -k "not e2e"` and `python -m pytest tests/ -q -k "memory or snapshot"`
Expected: PASS (optional params default to today's behavior).

- [ ] **Step 5: commit**

```bash
git add orchestrator/main.py tool/tool_memory.py tests/test_orchestrator/test_gateway_env_threading.py
git commit -m "feat(orchestrator): _build_orchestrator_llm(cfg) + snapshot_for_system_prompt(user) explicit overrides

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `max_concurrency()` + gateway semaphore (still default-serialized)

**Files:** `gateway/runner.py`; Test: `tests/test_gateway/`.

- [ ] **Step 1: failing test**

```python
# tests/test_gateway/test_gateway_concurrency.py
from __future__ import annotations

import importlib


def test_max_concurrency_default_and_override(monkeypatch):
    from gateway import runner
    monkeypatch.delenv("GATEWAY_MAX_CONCURRENCY", raising=False)
    importlib.reload(runner)
    assert runner.max_concurrency() == 1
    monkeypatch.setenv("GATEWAY_MAX_CONCURRENCY", "5")
    assert runner.max_concurrency() == 5
    monkeypatch.setenv("GATEWAY_MAX_CONCURRENCY", "garbage")
    assert runner.max_concurrency() == 1
```

- [ ] **Step 2: run → FAIL** (`runner` has no `max_concurrency`).

- [ ] **Step 3: implement** in `gateway/runner.py`:

```python
def max_concurrency() -> int:
    """Max simultaneous gateway turns. Default 1 = today's serialized behavior
    (reversible rollout); raise GATEWAY_MAX_CONCURRENCY to enable multi-user
    parallelism now that per-turn state lives on the TurnContext."""
    try:
        return max(1, int(os.environ.get("GATEWAY_MAX_CONCURRENCY", "1")))
    except ValueError:
        return 1
```

Replace `_CONCURRENCY_GUARD = asyncio.Lock()` with:
```python
# Bounded gateway concurrency. Default 1 reproduces the old single-lock behavior.
_GATEWAY_SEMAPHORE = asyncio.Semaphore(max_concurrency())
```
In `run_turn`, change `async with _CONCURRENCY_GUARD:` to `async with _GATEWAY_SEMAPHORE:`. Update the `run_turn` docstring's "one turn at a time per process" note to reflect the semaphore + per-turn-id runtime dirs.

- [ ] **Step 4: keep the feishu_ws import working.** `gateway/feishu_ws.py` imports `_CONCURRENCY_GUARD`? Grep — it only *references it in a comment*, not imports. Confirm `grep -n "_CONCURRENCY_GUARD" gateway/feishu_ws.py` shows only comments; no code change needed here yet.

- [ ] **Step 5: run** `python -m pytest tests/test_gateway/ -q` → PASS (semaphore(1) == lock).

- [ ] **Step 6: commit**

```bash
git add gateway/runner.py tests/test_gateway/test_gateway_concurrency.py
git commit -m "feat(gateway): GATEWAY_MAX_CONCURRENCY semaphore replaces the asyncio turn lock

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: drive the turn from `ctx` and drop `scoped_env` (the isolation win)

**Files:** `gateway/runner.py`; Test: `tests/test_gateway/`.

- [ ] **Step 1: AUDIT.** Confirm the only in-process per-turn-env readers are the two from Task 1:

Run: `grep -rn "os.environ.get(\"LANGCHAIN_AGENT_\|os.getenv(\"LANGCHAIN_AGENT_" gateway/ orchestrator/ | grep -v turn_context | grep -v test`
Expected: the readers are `_resolve_base_config` (model), `get_api_key`/`build_llm` (key) — all reached via `resolve_config(ctx)` now — and `tool_memory` (memory user, now param). If anything ELSE reads a per-turn var in the in-process turn path, thread it through `ctx` before proceeding. Record the result in the commit message.

- [ ] **Step 2: failing test — isolation (no env mutation)**

```python
# append to tests/test_gateway/test_gateway_concurrency.py
import asyncio
import os

import pytest


@pytest.mark.asyncio
async def test_run_turn_does_not_mutate_process_env(monkeypatch):
    from gateway import runner

    monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_MODEL", raising=False)

    captured = {}

    # Stub the heavy bits: capture the planner cfg + memory user, skip real spawn.
    async def fake_bootstrap(host, router):
        return None

    def fake_build_planner(router, *, context_text="", cfg=None):
        captured["cfg_model"] = getattr(cfg, "model", None)
        return runner._stub_planner

    def fake_planner_context(session_key, *, memory_user=""):
        captured["memory_user"] = memory_user
        return "", ""

    monkeypatch.setattr(runner, "_bootstrap", fake_bootstrap, raising=False)
    monkeypatch.setattr(runner, "_build_planner", fake_build_planner)
    monkeypatch.setattr(runner, "_build_planner_context", fake_planner_context)
    # Stub dispatch so the turn resolves to a prose reply quickly.
    async def fake_dispatch(*a, **k):
        return "ok"
    monkeypatch.setattr(runner, "_dispatch_decision", fake_dispatch)

    await runner.run_turn("hello", session_key="s", user_id="alice")

    # The per-turn memory user reached the snapshot via ctx, NOT os.environ.
    assert captured["memory_user"] == "alice"
    assert "LANGCHAIN_AGENT_MEMORY_USER" not in os.environ
    assert "LANGCHAIN_AGENT_MODEL" not in os.environ
```

- [ ] **Step 3: run → FAIL** (today `_build_planner` takes no `cfg`, `_build_planner_context` no `memory_user`, and `scoped_env` sets the env vars).

- [ ] **Step 4: implement** in `gateway/runner.py`:
  - `_build_planner(router, *, context_text="", cfg=None)` → pass `cfg` to `_build_orchestrator_llm(cfg)`.
  - `_build_planner_context(session_key, *, memory_user="")` → `snapshot_for_system_prompt(user=memory_user or None)`.
  - In `_run_turn_locked`: after building `ctx`, add `from config import resolve_config` and `cfg = resolve_config(ctx)`. **Delete** the `with scoped_env(ctx.turn_env()):` wrapper (dedent its body). Call `_build_planner_context(session_key, memory_user=ctx.user_id)` and `_build_planner(router, context_text=full_context, cfg=cfg)`. The host still gets `MCPHost(turn_env=ctx.turn_env())` (unchanged). Keep the `finally` (host shutdown, session append, `rmtree(ctx.runtime_dir)`).

- [ ] **Step 5: run** `python -m pytest tests/test_gateway/ -q` → PASS.

- [ ] **Step 6: e2e** (gateway spawns real specialists):

Run: `python -m pytest -m e2e -k "tool_task or simple_tool or legacy or comm" -q`
Expected: PASS.

- [ ] **Step 7: commit**

```bash
git add gateway/runner.py tests/test_gateway/test_gateway_concurrency.py
git commit -m "refactor(gateway): drive planner config + memory from ctx; drop scoped_env

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: feishu_ws threading lock → bounded semaphore

**Files:** `gateway/feishu_ws.py`; Test: `tests/test_gateway/`.

- [ ] **Step 1: implement.** In `gateway/feishu_ws.py`, replace `_dispatch_lock = threading.Lock()` with:

```python
from gateway import runner as _runner

# Bound concurrent dispatch across SDK worker threads. Default 1 = the old
# single-lock behavior; GATEWAY_MAX_CONCURRENCY>1 lets independent inbound
# messages run in parallel (each turn is isolated via its TurnContext).
_dispatch_sem = threading.BoundedSemaphore(_runner.max_concurrency())
```

Replace the `with _dispatch_lock:` use site with `with _dispatch_sem:`. Update the two-lock comment to describe the two *semaphores*.

- [ ] **Step 2: failing/locking test**

```python
# append to tests/test_gateway/test_gateway_concurrency.py
def test_feishu_ws_dispatch_semaphore_sized_from_flag(monkeypatch):
    import importlib

    monkeypatch.setenv("GATEWAY_MAX_CONCURRENCY", "3")
    from gateway import runner, feishu_ws
    importlib.reload(runner)
    importlib.reload(feishu_ws)
    # BoundedSemaphore admits exactly N concurrent holders.
    got = []
    for _ in range(3):
        assert feishu_ws._dispatch_sem.acquire(blocking=False)
        got.append(1)
    assert feishu_ws._dispatch_sem.acquire(blocking=False) is False  # 4th blocked
    for _ in got:
        feishu_ws._dispatch_sem.release()
    # restore default for later tests
    monkeypatch.delenv("GATEWAY_MAX_CONCURRENCY", raising=False)
    importlib.reload(runner)
    importlib.reload(feishu_ws)
```

- [ ] **Step 3: run** `python -m pytest tests/test_gateway/ -q` → PASS.

- [ ] **Step 4: commit**

```bash
git add gateway/feishu_ws.py tests/test_gateway/test_gateway_concurrency.py
git commit -m "feat(gateway): feishu_ws dispatch lock -> GATEWAY_MAX_CONCURRENCY bounded semaphore

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: concurrency proof + full-suite gate

**Files:** Test: `tests/test_gateway/`; verification.

- [ ] **Step 1: failing test — two turns overlap when the limit is raised**

```python
# append to tests/test_gateway/test_gateway_concurrency.py
@pytest.mark.asyncio
async def test_two_turns_overlap_when_limit_raised(monkeypatch):
    import importlib

    monkeypatch.setenv("GATEWAY_MAX_CONCURRENCY", "2")
    from gateway import runner
    importlib.reload(runner)

    both_in = asyncio.Semaphore(0)

    async def _wait_two(sem):
        await sem.acquire(); await sem.acquire()
        sem.release(); sem.release()

    async def fake_locked(prompt, **kw):
        both_in.release()
        await asyncio.wait_for(_wait_two(both_in), timeout=2.0)  # proves overlap
        return prompt

    monkeypatch.setattr(runner, "_run_turn_locked", fake_locked)
    results = await asyncio.gather(
        runner.run_turn("a", session_key="a"),
        runner.run_turn("b", session_key="b"),
    )
    assert set(results) == {"a", "b"}
    monkeypatch.delenv("GATEWAY_MAX_CONCURRENCY", raising=False)
    importlib.reload(runner)
```

- [ ] **Step 2: run → PASS** (with default 1 the same test would deadlock/timeout — proves the flag does something).

- [ ] **Step 3: full suite**

Run: `python -m pytest -q`
Expected: all pass (prior gate: 840 passed, 1 skipped; gateway tests add to the count).

- [ ] **Step 4: confirm default is still serialized**

Run: `python -c "import importlib; from gateway import runner; importlib.reload(runner); print(runner.max_concurrency())"` → `1`

- [ ] **Step 5: commit**

```bash
git add tests/test_gateway/test_gateway_concurrency.py
git commit -m "test(gateway): prove two turns overlap at GATEWAY_MAX_CONCURRENCY=2

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Notes

- **Reversibility:** `GATEWAY_MAX_CONCURRENCY` unset/=1 is byte-for-byte today's behavior (semaphore of 1 == lock; env still threaded but reads identical).
- **Enable:** `GATEWAY_MAX_CONCURRENCY=5 python -m gateway ...`.
- **Out of scope:** warm pool (gateway keeps per-turn cold spawn).
