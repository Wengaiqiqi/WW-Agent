# Phase A: TurnContext foundation + multi-user concurrency — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace process-global per-turn `os.environ` state (and the single `_CONCURRENCY_GUARD`) with an explicit `TurnContext`, so multiple web users' turns run concurrently with correct isolation.

**Architecture:** A frozen `TurnContext` is built at each entry point (CLI/gateway/web) — the only place env/request is read. In-process config resolves from the context; specialist subprocesses get their per-turn config via an explicit `turn_env` dict passed to `MCPHost` (never via mutating `os.environ`); runtime-discovery dirs are keyed per `turn_id`. The global concurrency guard is removed behind a `WEB_MAX_CONCURRENCY` flag (default 1 = today's behavior) so rollout is reversible.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, pytest / pytest-asyncio. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-01-multi-user-concurrency-turncontext-design.md` (Phase A).

**Scope of THIS plan:** Phase A only (A1 behavior-preserving foundation + A2 concurrency enablement). Phases B (warm-pool), C (security gate), D (heuristics) are separate plans.

---

## File Structure

**Create:**
- `orchestrator/turn_context.py` — the `TurnContext` dataclass + `from_env()` + `turn_env()`.
- `tests/test_orchestrator/test_turn_context.py` — unit tests for the above.
- `tests/test_web/test_concurrency_isolation.py` — parallel-turn isolation test.

**Modify:**
- `config/_providers.py` — add `api_key: str = ""` field to `ActiveConfig`.
- `config/_credentials.py` — `get_api_key` prefers `cfg.api_key` over the `LANGCHAIN_AGENT_API_KEY` env override.
- `config/_settings.py` — add `resolve_config(ctx)`; `load_active_config()` becomes a thin shim.
- `orchestrator/mcp_host.py` — `MCPHost` accepts a `turn_env` overlay; `_build_agent_env` merges it instead of reading per-turn vars from `os.environ`.
- `gateway/runner.py` — build a `TurnContext`; pass `turn_env` to `MCPHost`; per-`turn_id` runtime dir; stop mutating per-turn env.
- `web/bridge.py` — same as runner, plus the `WEB_MAX_CONCURRENCY` guard change.
- `web/config.py` — add `max_concurrency()` reader.

---

## Task 1: `TurnContext` dataclass

**Files:**
- Create: `orchestrator/turn_context.py`
- Test: `tests/test_orchestrator/test_turn_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator/test_turn_context.py
from __future__ import annotations

from pathlib import Path

from orchestrator.turn_context import TurnContext


def test_turn_env_emits_only_nonempty_per_turn_vars():
    ctx = TurnContext(
        turn_id="t1", user_id="alice", workspace_root=Path("/ws"),
        permission_mode="workspace-write", model_id="custom/gpt",
        base_url="https://api.x/v1", api_key="sk-1", protocol="openai",
        session_key="conv1", trace_id="tr1", hmac_key="h1",
        runtime_dir=Path("/rt"),
    )
    env = ctx.turn_env()
    assert env["LANGCHAIN_AGENT_MEMORY_USER"] == "alice"
    assert env["LANGCHAIN_AGENT_WORKSPACE_ROOT"] == "/ws"
    assert env["LANGCHAIN_AGENT_PERMISSION_MODE"] == "workspace-write"
    assert env["LANGCHAIN_AGENT_MODEL"] == "custom/gpt"
    assert env["LANGCHAIN_AGENT_BASE_URL"] == "https://api.x/v1"
    assert env["LANGCHAIN_AGENT_API_KEY"] == "sk-1"
    assert env["LANGCHAIN_AGENT_PROTOCOL"] == "openai"
    assert env["LANGCHAIN_AGENT_RUNTIME_DIR"] == "/rt"


def test_turn_env_omits_empty_optionals():
    ctx = TurnContext(
        turn_id="t2", user_id="", workspace_root=Path("/ws"),
        permission_mode="read-only", model_id="", base_url="", api_key="",
        protocol="", session_key="", trace_id="tr2", hmac_key="h2",
        runtime_dir=Path("/rt2"),
    )
    env = ctx.turn_env()
    # Empty optionals are absent (not set to "") so they don't clobber a child default.
    for absent in ("LANGCHAIN_AGENT_MEMORY_USER", "LANGCHAIN_AGENT_MODEL",
                   "LANGCHAIN_AGENT_BASE_URL", "LANGCHAIN_AGENT_API_KEY",
                   "LANGCHAIN_AGENT_PROTOCOL"):
        assert absent not in env
    # Required-always vars are present.
    assert env["LANGCHAIN_AGENT_PERMISSION_MODE"] == "read-only"
    assert env["LANGCHAIN_AGENT_WORKSPACE_ROOT"] == "/ws"


def test_from_env_reads_current_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "bob")
    monkeypatch.setenv("LANGCHAIN_AGENT_MODEL", "deepseek/chat")
    monkeypatch.setenv("LANGCHAIN_AGENT_PERMISSION_MODE", "danger-full-access")
    monkeypatch.delenv("LANGCHAIN_AGENT_BASE_URL", raising=False)
    ctx = TurnContext.from_env(session_key="s", trace_id="tr", hmac_key="h",
                               runtime_dir=tmp_path)
    assert ctx.user_id == "bob"
    assert ctx.model_id == "deepseek/chat"
    assert ctx.permission_mode == "danger-full-access"
    assert ctx.base_url == ""
    assert ctx.turn_id  # auto-generated, non-empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_turn_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.turn_context'`

- [ ] **Step 3: Write the implementation**

```python
# orchestrator/turn_context.py
"""The explicit per-turn context that replaces process-global os.environ state.

Built once at each entry point (CLI / gateway / web) — the ONLY place env or
request data is read into the orchestrator. Everything downstream takes the
context (or a config resolved from it) explicitly, so two turns running
concurrently never share mutable global state.

``turn_env()`` is the cross-process channel: the dict handed to MCPHost and
merged into each spawned specialist's environment. The parent process's
``os.environ`` is never mutated.
"""
from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TurnContext:
    turn_id: str
    user_id: str
    workspace_root: Path
    permission_mode: str
    model_id: str
    base_url: str
    api_key: str
    protocol: str
    session_key: str
    trace_id: str
    hmac_key: str
    runtime_dir: Path

    def turn_env(self) -> dict[str, str]:
        """The per-turn env overlay handed to spawned subprocesses. Empty
        optionals are omitted so a child default isn't clobbered with ""; the
        always-present vars (permission mode, workspace, runtime dir) are set
        unconditionally."""
        env: dict[str, str] = {
            "LANGCHAIN_AGENT_PERMISSION_MODE": self.permission_mode,
            "LANGCHAIN_AGENT_WORKSPACE_ROOT": str(self.workspace_root),
            "LANGCHAIN_AGENT_RUNTIME_DIR": str(self.runtime_dir),
        }
        optionals = {
            "LANGCHAIN_AGENT_MEMORY_USER": self.user_id,
            "LANGCHAIN_AGENT_MODEL": self.model_id,
            "LANGCHAIN_AGENT_BASE_URL": self.base_url,
            "LANGCHAIN_AGENT_API_KEY": self.api_key,
            "LANGCHAIN_AGENT_PROTOCOL": self.protocol,
        }
        env.update({k: v for k, v in optionals.items() if v})
        return env

    @classmethod
    def from_env(
        cls,
        *,
        session_key: str = "",
        trace_id: str = "",
        hmac_key: str = "",
        runtime_dir: Path | None = None,
        workspace_root: Path | None = None,
    ) -> "TurnContext":
        """Build a context from the current process env — the single-user CLI /
        legacy path. Reproduces today's env-based behavior so existing surfaces
        are unaffected."""
        return cls(
            turn_id=uuid.uuid4().hex,
            user_id=os.environ.get("LANGCHAIN_AGENT_MEMORY_USER", ""),
            workspace_root=workspace_root or Path(
                os.environ.get("LANGCHAIN_AGENT_WORKSPACE_ROOT", "") or os.getcwd()
            ),
            permission_mode=os.environ.get(
                "LANGCHAIN_AGENT_PERMISSION_MODE", "danger-full-access"
            ),
            model_id=os.environ.get("LANGCHAIN_AGENT_MODEL", ""),
            base_url=os.environ.get("LANGCHAIN_AGENT_BASE_URL", ""),
            api_key=os.environ.get("LANGCHAIN_AGENT_API_KEY", ""),
            protocol=os.environ.get("LANGCHAIN_AGENT_PROTOCOL", ""),
            session_key=session_key,
            trace_id=trace_id or uuid.uuid4().hex[:8],
            hmac_key=hmac_key or secrets.token_urlsafe(32),
            runtime_dir=runtime_dir or Path(".agent") / "runtime" / "cli",
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator/test_turn_context.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/turn_context.py tests/test_orchestrator/test_turn_context.py
git commit -m "feat(orchestrator): add TurnContext — explicit per-turn state carrier

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Route the API key through `cfg` instead of the env override

**Files:**
- Modify: `config/_providers.py` (the `ActiveConfig` dataclass, ~line 441)
- Modify: `config/_credentials.py:82-92` (`get_api_key`)
- Test: `tests/test_config_overrides.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_config_overrides.py
from config._providers import make_config


def test_get_api_key_prefers_cfg_api_key_over_env(monkeypatch):
    from config._credentials import get_api_key
    monkeypatch.setenv("LANGCHAIN_AGENT_API_KEY", "env-key")
    cfg = make_config("deepseek")  # any real provider
    cfg.api_key = "ctx-key"  # ActiveConfig is a non-frozen dataclass
    # The explicit per-turn key on cfg wins; the global env override is no longer consulted.
    assert get_api_key(cfg) == "ctx-key"


def test_get_api_key_without_cfg_key_falls_back_to_provider_env(monkeypatch):
    from config._credentials import get_api_key
    monkeypatch.delenv("LANGCHAIN_AGENT_API_KEY", raising=False)
    cfg = make_config("deepseek")
    monkeypatch.setenv(cfg.api_key_env, "provider-key")
    assert get_api_key(cfg) == "provider-key"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_overrides.py -k api_key -v`
Expected: FAIL — `test_get_api_key_prefers_cfg_api_key_over_env` fails because `ActiveConfig` has no `api_key` attribute / the env override still wins.

- [ ] **Step 3: Add the field to `ActiveConfig`**

In `config/_providers.py`, add a field to the `ActiveConfig` dataclass (after `protocol: str`, before `temperature`):

```python
    protocol: str
    # Resolved literal API key for THIS turn (web custom-endpoint flow sets it
    # via TurnContext). Empty = fall back to api_key_env / credentials file.
    api_key: str = ""
    temperature: float = DEFAULT_TEMPERATURE
```

- [ ] **Step 4: Update `get_api_key`**

In `config/_credentials.py`, replace the body of `get_api_key` (lines 82-92):

```python
def get_api_key(cfg: ActiveConfig) -> str:
    """Look up the API key for *cfg*.

    A literal ``cfg.api_key`` (set per-turn from the TurnContext, e.g. the web
    custom-endpoint flow) wins; otherwise fall back to the provider's
    ``api_key_env`` then the credentials file. NOTE: this no longer reads the
    ``LANGCHAIN_AGENT_API_KEY`` env var — per-turn keys travel on the context,
    not process-global env, so a parallel turn can't pick up another's key.
    """
    if getattr(cfg, "api_key", ""):
        return cfg.api_key
    return os.getenv(cfg.api_key_env) or load_credentials().get(cfg.api_key_env, "")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config_overrides.py -v`
Expected: PASS

- [ ] **Step 6: Run the broader config + credential suites for regressions**

Run: `python -m pytest tests/test_config_overrides.py tests/test_gateway/test_credentials.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add config/_providers.py config/_credentials.py tests/test_config_overrides.py
git commit -m "feat(config): carry per-turn api_key on ActiveConfig, drop env override in get_api_key

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `resolve_config(ctx)` and shim `load_active_config()`

**Files:**
- Modify: `config/_settings.py` (`load_active_config`, `_resolve_base_config`, add `resolve_config`)
- Test: `tests/test_config_overrides.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_config_overrides.py
from pathlib import Path

from orchestrator.turn_context import TurnContext


def _ctx(**over):
    base = dict(turn_id="t", user_id="", workspace_root=Path("/ws"),
                permission_mode="workspace-write", model_id="", base_url="",
                api_key="", protocol="", session_key="", trace_id="tr",
                hmac_key="h", runtime_dir=Path("/rt"))
    base.update(over)
    return TurnContext(**base)


def test_resolve_config_applies_ctx_overrides(monkeypatch):
    from config._settings import resolve_config
    cfg = resolve_config(_ctx(model_id="deepseek/deepseek-chat",
                              base_url="https://api.x/v1",
                              api_key="sk-ctx", protocol="openai"))
    assert cfg.base_url == "https://api.x/v1"
    assert cfg.api_key == "sk-ctx"
    assert cfg.protocol == "openai"


def test_resolve_config_rejects_unknown_protocol(monkeypatch):
    from config._settings import resolve_config
    cfg = resolve_config(_ctx(model_id="deepseek/deepseek-chat", protocol="gemini"))
    # Unknown protocol is ignored (kept as resolved default), per the whitelist.
    assert cfg.protocol != "gemini"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_overrides.py -k resolve_config -v`
Expected: FAIL — `cannot import name 'resolve_config'`

- [ ] **Step 3: Implement `resolve_config` and refactor `_resolve_base_config`**

In `config/_settings.py`, add a model-id parameter to base resolution and a context-driven resolver. Replace `_resolve_base_config` and `load_active_config`:

```python
def load_active_config() -> ActiveConfig:
    """Resolve the active model from process env + settings.json. Thin shim over
    ``resolve_config`` for the single-user CLI / legacy path; building a
    ``TurnContext`` from env reproduces the prior env-driven behavior."""
    from orchestrator.turn_context import TurnContext
    return resolve_config(TurnContext.from_env())


def resolve_config(ctx) -> ActiveConfig:
    """Resolve an ActiveConfig from an explicit TurnContext (no os.environ reads
    beyond the settings.json base). ctx overrides win over settings.json."""
    cfg = _resolve_base_config(ctx.model_id)
    if ctx.base_url:
        cfg.base_url = ctx.base_url
    if ctx.protocol:
        if ctx.protocol in _KNOWN_PROTOCOLS:
            cfg.protocol = ctx.protocol
        else:
            logger.warning(
                "Ignoring protocol %r from context: unknown (expected %s).",
                ctx.protocol, sorted(_KNOWN_PROTOCOLS),
            )
    if ctx.api_key:
        cfg.api_key = ctx.api_key
    return cfg


def _resolve_base_config(model_choice: str = "") -> ActiveConfig:
    """Resolve provider+model from an explicit model choice (``provider/model``
    or ``provider``) falling back to settings.json then DEFAULT_PROVIDER. The
    explicit arg replaces the prior direct ``LANGCHAIN_AGENT_MODEL`` env read so
    the source is the caller's TurnContext, not process-global env."""
    env_choice = (model_choice or "").strip()
    if env_choice:
        if "/" in env_choice:
            prov_name, model_name = env_choice.split("/", 1)
        else:
            prov_name, model_name = env_choice, ""
        if prov_name in PROVIDERS:
            return make_config(prov_name, model=model_name)

    settings = _read_settings()
    model_block = settings.get("model")
    if isinstance(model_block, dict):
        prov_name = str(model_block.get("provider") or "")
        if prov_name in PROVIDERS:
            return make_config(
                prov_name,
                model=str(model_block.get("model") or ""),
                base_url=str(model_block.get("base_url") or ""),
                api_key_env=str(model_block.get("api_key_env") or ""),
            )
    elif isinstance(model_block, str) and model_block:
        logger.warning(
            "Ignoring legacy settings.json model entry %r; the schema is now a "
            "dict. Run /model to reconfigure (falling back to provider %r).",
            model_block, DEFAULT_PROVIDER,
        )
    return make_config(DEFAULT_PROVIDER)
```

Then DELETE the now-unused `_apply_env_overrides` function (its protocol-whitelist logic moved into `resolve_config`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config_overrides.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite (load_active_config has many callers)**

Run: `python -m pytest -q -k "not e2e"`
Expected: PASS (no behavior change — `load_active_config` still reads env via `from_env`)

- [ ] **Step 6: Commit**

```bash
git add config/_settings.py tests/test_config_overrides.py
git commit -m "feat(config): resolve_config(ctx); load_active_config becomes a from_env shim

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `MCPHost` accepts a per-turn env overlay

**Files:**
- Modify: `orchestrator/mcp_host.py` (`MCPHost.__init__`, `_build_agent_env`, the `spawn` call site that uses it)
- Test: `tests/test_orchestrator/test_mcp_host.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_orchestrator/test_mcp_host.py
from orchestrator.mcp_host import MCPHost, _build_agent_env


def test_build_agent_env_uses_turn_env_overlay_not_os_environ(monkeypatch):
    # A per-turn value provided via the overlay must win and must NOT require
    # the parent os.environ to be mutated.
    monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", raising=False)
    overlay = {
        "LANGCHAIN_AGENT_MEMORY_USER": "alice",
        "LANGCHAIN_AGENT_WORKSPACE_ROOT": "/ws/alice",
        "LANGCHAIN_AGENT_RUNTIME_DIR": "/rt/t1",
    }
    env = _build_agent_env(hmac_key="h", agent_id="tool-agent", turn_env=overlay)
    assert env["LANGCHAIN_AGENT_MEMORY_USER"] == "alice"
    assert env["LANGCHAIN_AGENT_WORKSPACE_ROOT"] == "/ws/alice"
    assert env["LANGCHAIN_AGENT_RUNTIME_DIR"] == "/rt/t1"
    # The parent process env was not touched.
    assert "LANGCHAIN_AGENT_MEMORY_USER" not in os.environ


def test_build_agent_env_overlay_overrides_parent_env(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_AGENT_MODEL", "parent/model")
    env = _build_agent_env(hmac_key="h", agent_id="tool-agent",
                           turn_env={"LANGCHAIN_AGENT_MODEL": "turn/model"})
    assert env["LANGCHAIN_AGENT_MODEL"] == "turn/model"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_mcp_host.py -k turn_env -v`
Expected: FAIL — `_build_agent_env() got an unexpected keyword argument 'turn_env'`

- [ ] **Step 3: Add the `turn_env` parameter**

In `orchestrator/mcp_host.py`, change `_build_agent_env`'s signature and merge the overlay last (so it wins over any inherited parent value):

```python
def _build_agent_env(
    *, hmac_key: str, agent_id: str, turn_env: dict[str, str] | None = None
) -> dict[str, str]:
```

At the end of the function, just before `return env`, after the existing
`env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"` line, merge the
overlay (the overlay's permission mode, if present, wins):

```python
    if turn_env:
        # Per-turn config travels explicitly (TurnContext), overriding both the
        # inherited parent env and the workspace-write default above. This is
        # what lets parallel turns spawn specialists with different
        # user/workspace/model/key without mutating the shared os.environ.
        env.update(turn_env)
    return env
```

- [ ] **Step 4: Thread it through `MCPHost`**

In `MCPHost.__init__`, accept and store the overlay:

```python
    def __init__(self, *, hmac_key: str, turn_env: dict[str, str] | None = None):
        self._hmac_key = hmac_key
        self._turn_env = turn_env or {}
        self._clients: dict[str, _ClientHandle] = {}
```

Find the `_build_agent_env(...)` call inside `spawn` (Grep `_build_agent_env(` in this file) and pass the overlay:

```python
        env = _build_agent_env(
            hmac_key=self._hmac_key, agent_id=card.id, turn_env=self._turn_env,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_orchestrator/test_mcp_host.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add orchestrator/mcp_host.py tests/test_orchestrator/test_mcp_host.py
git commit -m "feat(mcp_host): accept per-turn env overlay instead of reading per-turn os.environ

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Gateway builds a `TurnContext` and stops mutating per-turn env

**Files:**
- Modify: `gateway/runner.py` (`_run_turn_locked`, `_build_planner`, `_dispatch_decision`)
- Test: `tests/test_gateway/` (existing suite is the regression guard)

- [ ] **Step 1: Build the context and pass `turn_env` to the host**

In `gateway/runner.py` `_run_turn_locked`, replace the `scoped_env({"LANGCHAIN_AGENT_MEMORY_USER": ...})` + `private_runtime_dir("gw")` block with a context-built version. Build the context first, derive the runtime dir from `turn_id`, create the host with the overlay, and resolve the planner config from the context:

```python
    from orchestrator.turn_context import TurnContext

    ctx = TurnContext.from_env(
        session_key=session_key, trace_id=trace_id,
        hmac_key=secrets.token_urlsafe(32),
        runtime_dir=Path(".agent") / "runtime" / "gw",  # replaced per-turn below
    )
    # Per-turn-id runtime dir (was per-PID) so parallel turns can't collide.
    gw_runtime = Path(".agent") / "runtime" / f"gw-{ctx.turn_id}"
    ctx = replace(ctx, runtime_dir=gw_runtime)

    host = MCPHost(hmac_key=ctx.hmac_key, turn_env=ctx.turn_env())
    router = CapabilityRouter()
```

Add `from dataclasses import replace` and ensure `from pathlib import Path` at the top of the module (Path import already exists from the earlier cleanup).

The memory-user env must still be visible to `_build_planner_context` (it reads the memory file via `snapshot_for_system_prompt`, which keys off `LANGCHAIN_AGENT_MEMORY_USER`). Because that read happens **in-process**, wrap just that call in `scoped_env(ctx.turn_env())` for the in-process duration (the subprocess path already gets the overlay via the host):

```python
    with scoped_env(ctx.turn_env()):
        history_context, full_context = _build_planner_context(session_key)
        await _bootstrap(host, router)
        ...  # slash, planner, dispatch as before
```

The surrounding `private_runtime_dir` is removed (the runtime dir now lives in `ctx.turn_env()` and reaches subprocesses via the host; the in-process `scoped_env(ctx.turn_env())` covers in-process readers). Keep the `finally` that does `host.shutdown_all()` + `session_store.append(...)`, and add an explicit `shutil.rmtree(object_runtime, ignore_errors=True)` in that finally.

- [ ] **Step 2: Resolve the planner config from the context**

`_build_planner(router, *, context_text)` currently calls `_build_orchestrator_llm()` which calls `load_active_config()` (reads env). Inside the `scoped_env(ctx.turn_env())` block this still works (env is set for the in-process window), so **no change is required for behavior parity**. Leave `_build_planner` as-is for this task; Phase B revisits it for the pool.

- [ ] **Step 3: Run the gateway suite**

Run: `python -m pytest tests/test_gateway/ -q`
Expected: PASS (gateway behavior unchanged — the env is still set in-process for the turn window; only the *source of truth* moved to the context and the subprocess channel is now explicit)

- [ ] **Step 4: Run e2e (gateway path spawns real specialists)**

Run: `python -m pytest -m e2e -k "tool_task or simple_tool or legacy" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/runner.py
git commit -m "refactor(gateway): drive turn from TurnContext; per-turn-id runtime dir; explicit spawn env

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Web bridge builds a `TurnContext`; per-turn-id runtime dir

**Files:**
- Modify: `web/bridge.py` (`_run_streaming_locked`, `_web_turn_env`)
- Test: `tests/test_web/test_bridge.py` (existing), `tests/test_web/test_app.py` (existing)

- [ ] **Step 1: Build the context in `_run_streaming_locked`**

In `web/bridge.py` `_run_streaming_locked`, build a `TurnContext` from the call args (the web route already passes user_id/model_id/base_url/api_key/protocol), key the runtime dir per `turn_id`, and create pooled-or-spawned hosts with the overlay. Replace the `_web_turn_env(...) + private_runtime_dir("web")` wrapper:

```python
    from dataclasses import replace
    from orchestrator.turn_context import TurnContext

    ctx = TurnContext(
        turn_id=secrets.token_hex(8),
        user_id=user_id,
        workspace_root=_user_workspace(user_id),
        permission_mode=config.WEB_PERMISSION_MODE,
        model_id=model_id, base_url=base_url, api_key=api_key, protocol=protocol,
        session_key=session_key, trace_id=trace_id,
        hmac_key=secrets.token_urlsafe(32),
        runtime_dir=Path(".agent") / "runtime" / "web",  # set below
    )
    ctx = replace(ctx, runtime_dir=Path(".agent") / "runtime" / f"web-{ctx.turn_id}")

    async def ensure_specialists() -> tuple[Any, Any]:
        nonlocal host, router
        from orchestrator.main import _bootstrap
        from orchestrator.mcp_host import MCPHost
        from orchestrator.router import CapabilityRouter
        if host is None:
            host = MCPHost(hmac_key=ctx.hmac_key, turn_env=ctx.turn_env())
            router = CapabilityRouter()
            await _bootstrap(host, router)
        return host, router

    final_text = ""
    with scoped_env(ctx.turn_env()):   # in-process readers (planner ctx, catalog)
        try:
            history_context, full_context = _build_planner_context(session_key)
            capabilities, tool_schemas = await _capability_catalog()
            planner = _build_planner(
                _CatalogRouter(capabilities, tool_schemas), context_text=full_context,
            )
            async for ev in _plan_and_dispatch(
                planner, prompt=prompt, ensure_specialists=ensure_specialists,
                hmac_key=ctx.hmac_key, trace_id=trace_id,
                history_context=history_context,
            ):
                if ev.get("type") == "text":
                    final_text += ev.get("chunk", "")
                elif ev.get("type") == "done" and ev.get("text"):
                    final_text = ev["text"]
                yield ev
        except Exception as exc:  # noqa: BLE001
            log.exception("web: planner/dispatch failed")
            yield {"type": "error", "message": f"planner: {exc}"}
            yield {"type": "done", "text": ""}
        finally:
            if host is not None:
                await host.shutdown_all()
            if session_key and final_text:
                session_store.append(session_key, prompt, final_text)
            import shutil
            shutil.rmtree(ctx.runtime_dir, ignore_errors=True)
```

Update `hmac_key` references later in the function to `ctx.hmac_key`. The `_web_turn_env` helper is now unused by this path — DELETE it (Grep `_web_turn_env` to confirm no other callers).

- [ ] **Step 2: Run the web bridge + app suites**

Run: `python -m pytest tests/test_web/ -q`
Expected: PASS. If a test asserts on `_web_turn_env` directly, rewrite it to assert on `ctx.turn_env()` (Grep `_web_turn_env` in `tests/`).

- [ ] **Step 3: Commit**

```bash
git add web/bridge.py tests/test_web/
git commit -m "refactor(web): drive turn from TurnContext; per-turn-id runtime dir; explicit spawn env

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Remove the global concurrency guard behind `WEB_MAX_CONCURRENCY`

**Files:**
- Modify: `web/config.py` (add `max_concurrency()`)
- Modify: `web/bridge.py` (`run_turn_streaming` — replace the single global lock with a bounded semaphore)
- Test: `tests/test_web/test_concurrency_isolation.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web/test_concurrency_isolation.py
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_two_turns_run_concurrently_when_limit_raised(monkeypatch):
    """With WEB_MAX_CONCURRENCY>1 two turns overlap; with the old single guard
    they would have serialized. We prove overlap by having each turn block on a
    barrier that only releases once BOTH have entered."""
    monkeypatch.setenv("WEB_MAX_CONCURRENCY", "2")

    import importlib
    from web import config as web_config
    importlib.reload(web_config)  # re-read the env-driven limit
    from web import bridge
    importlib.reload(bridge)

    entered = asyncio.Event()
    both_in = asyncio.Semaphore(0)
    count = {"n": 0}

    async def fake_locked(prompt, **kw):
        count["n"] += 1
        both_in.release()
        # Wait until the other turn has also entered — proves concurrency.
        await asyncio.wait_for(_wait_two(both_in), timeout=2.0)
        yield {"type": "done", "text": prompt}

    async def _wait_two(sem):
        await sem.acquire()
        await sem.acquire()
        sem.release(); sem.release()

    monkeypatch.setattr(bridge, "_stream_off_loop",
                        lambda *a, **k: fake_locked(*a, **k))

    async def drain(p):
        return [e async for e in bridge.run_turn_streaming(p, session_key=p)]

    results = await asyncio.gather(drain("a"), drain("b"))
    assert {r[-1]["text"] for r in results} == {"a", "b"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web/test_concurrency_isolation.py -v`
Expected: FAIL (times out) — the single `_CONCURRENCY_GUARD` serializes, so the barrier never releases.

- [ ] **Step 3: Add the config reader**

In `web/config.py`:

```python
def max_concurrency() -> int:
    """Max simultaneous web turns. Default 1 = today's serialized behavior
    (reversible rollout); raise to enable multi-user parallelism."""
    try:
        return max(1, int(os.environ.get("WEB_MAX_CONCURRENCY", "1")))
    except ValueError:
        return 1
```

- [ ] **Step 4: Replace the guard with a bounded semaphore in `web/bridge.py`**

At module level in `web/bridge.py`, replace the import-and-use of `_CONCURRENCY_GUARD` with a local bounded semaphore sized from config:

```python
from web import config

# Bounded concurrency for web turns. Default 1 reproduces the old single-guard
# behavior; WEB_MAX_CONCURRENCY>1 lets independent turns run in parallel now
# that per-turn state lives on the TurnContext, not process-global env.
_TURN_SEMAPHORE = asyncio.Semaphore(config.max_concurrency())
```

In `run_turn_streaming`, replace `async with _CONCURRENCY_GUARD:` with `async with _TURN_SEMAPHORE:`. Remove the `_CONCURRENCY_GUARD` import from `gateway.runner`.

- [ ] **Step 5: Run the new test + full web suite**

Run: `python -m pytest tests/test_web/ -q`
Expected: PASS

- [ ] **Step 6: Confirm default is still serialized (no env set)**

Run: `python -c "import importlib; from web import config; importlib.reload(config); print(config.max_concurrency())"`
Expected: `1`

- [ ] **Step 7: Commit**

```bash
git add web/config.py web/bridge.py tests/test_web/test_concurrency_isolation.py
git commit -m "feat(web): bounded WEB_MAX_CONCURRENCY semaphore replaces single global turn guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Full-suite regression gate

**Files:** none (verification only)

- [ ] **Step 1: Run the entire suite**

Run: `python -m pytest -q`
Expected: all pass (the prior baseline was 776 passed, 1 skipped; new tests add to the pass count). If any test asserted on the removed `_apply_env_overrides` / `_web_turn_env` / `_CONCURRENCY_GUARD`, rewrite it to assert on `TurnContext` / `ctx.turn_env()` / `_TURN_SEMAPHORE` and re-run.

- [ ] **Step 2: Manual smoke (optional, if a provider key is configured)**

Run the web server at concurrency 2 and confirm two browser tabs from two accounts can hold turns simultaneously:
`WEB_MAX_CONCURRENCY=2 WEB_AUTH_SECRET=dev WEB_SIGNUP_CODE=dev python -m web --host 127.0.0.1`

- [ ] **Step 3: Commit any test fixups**

```bash
git add -A
git commit -m "test: update assertions for TurnContext-based per-turn state

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **TDD discipline:** every task writes the failing test first, watches it fail, then implements. Don't batch.
- **Behavior parity is the contract for A1 (Tasks 1–3, 5):** the in-process `scoped_env(ctx.turn_env())` window preserves today's behavior for env-reading in-process code (planner config, memory snapshot). The *source of truth* moved to the context and the *subprocess channel* is now explicit; nothing user-visible changes until Task 7 raises concurrency.
- **The win turns on in Task 7**, and only when `WEB_MAX_CONCURRENCY>1`. Default stays 1 so this whole plan is shippable with zero behavior change, then flipped on after validation.
- **Out of scope:** warm-pool (Phase B) — Tasks here still spawn/teardown per turn. The `MCPHost(turn_env=...)` seam from Task 4 is exactly what Phase B's pool keys on.
