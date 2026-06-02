from __future__ import annotations

import os
from pathlib import Path

from web import bridge


def test_web_turn_context_builds_turn_env(tmp_config_dir, monkeypatch):
    # The web turn no longer mutates os.environ; it builds a TurnContext whose
    # turn_env() is the explicit per-turn overlay (server-enforced workspace-write
    # tier, per-user workspace, selected model).
    monkeypatch.delenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_MODEL", raising=False)

    ctx = bridge._web_turn_context(
        user_id="u-alice", model_id="anthropic/claude-opus-4-7",
        session_key="s", trace_id="tr",
    )
    env = ctx.turn_env()
    assert env["LANGCHAIN_AGENT_PERMISSION_MODE"] == "workspace-write"
    assert env["LANGCHAIN_AGENT_MEMORY_USER"] == "u-alice"
    assert env["LANGCHAIN_AGENT_MODEL"] == "anthropic/claude-opus-4-7"
    # per-user workspace under the config dir, and it exists
    assert "u-alice" in env["LANGCHAIN_AGENT_WORKSPACE_ROOT"]
    assert Path(env["LANGCHAIN_AGENT_WORKSPACE_ROOT"]).is_dir()
    assert ctx.workspace_root == Path(env["LANGCHAIN_AGENT_WORKSPACE_ROOT"])
    # per-turn-id runtime dir so parallel turns don't collide
    assert ctx.turn_id and ctx.turn_id in env["LANGCHAIN_AGENT_RUNTIME_DIR"]

    # Building the context does NOT touch the parent process env.
    assert "LANGCHAIN_AGENT_WORKSPACE_ROOT" not in os.environ
    assert "LANGCHAIN_AGENT_MEMORY_USER" not in os.environ
    assert "LANGCHAIN_AGENT_MODEL" not in os.environ


def test_web_turn_context_two_users_isolated(tmp_config_dir):
    ctx_a = bridge._web_turn_context(user_id="u-alice", model_id="",
                                     session_key="s", trace_id="tr")
    ctx_b = bridge._web_turn_context(user_id="u-bob", model_id="",
                                     session_key="s", trace_id="tr")
    assert ctx_a.workspace_root != ctx_b.workspace_root
    assert "u-alice" in str(ctx_a.workspace_root)
    assert "u-bob" in str(ctx_b.workspace_root)
    # distinct per-turn runtime dirs too
    assert ctx_a.runtime_dir != ctx_b.runtime_dir


import asyncio

from web import bridge as bridge_mod


def _collect(agen):
    async def _run():
        out = []
        async for ev in agen:
            out.append(ev)
        return out
    return asyncio.run(_run())


def test_dispatch_branch_a_prose():
    decision = {"capability": "", "response": "  just text  "}
    events = _collect(bridge_mod.dispatch_decision_stream(
        decision=decision, prompt="hi", host=None, router=None,
        hmac_key="k", trace_id="t", history_context="", delegate=None,
    ))
    assert events == [
        {"type": "text", "chunk": "just text"},
        {"type": "done", "text": "just text"},
    ]


def test_dispatch_branch_b_forwards_a2a_events():
    a2a_events = [
        {"type": "thinking", "text": "hmm"},
        {"type": "text", "chunk": "ans"},
        {"type": "done", "text": "ans"},
    ]

    async def fake_delegate(*, peer_id, task, meta, context=""):
        for ev in a2a_events:
            yield ev

    decision = {"capability": "tool.task", "arguments": {"task": "do"}}
    events = _collect(bridge_mod.dispatch_decision_stream(
        decision=decision, prompt="do it", host=None, router=None,
        hmac_key="k", trace_id="t", history_context="ctx", delegate=fake_delegate,
    ))
    assert events == a2a_events


def test_dispatch_branch_b_error_event_emitted():
    async def fake_delegate(*, peer_id, task, meta, context=""):
        raise RuntimeError("kaboom")
        yield  # pragma: no cover

    decision = {"capability": "tool.task", "arguments": {}}
    events = _collect(bridge_mod.dispatch_decision_stream(
        decision=decision, prompt="x", host=None, router=None,
        hmac_key="k", trace_id="t", history_context="", delegate=fake_delegate,
    ))
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "error" and "kaboom" in e["message"] for e in events)


async def _ensure_must_not_be_called():
    """ensure_specialists stand-in that fails the test if a prose turn tries
    to spawn specialists."""
    raise AssertionError("prose turn must not spawn specialists")


def test_plan_and_dispatch_streams_prose_token_by_token():
    """A direct (prose) answer must stream token-by-token, not arrive as one
    blob — same as the CLI. It must also NOT spawn any specialist."""

    class FakePlanner:
        async def astream_plan(self, state):
            yield {"type": "text", "chunk": "你"}
            yield {"type": "text", "chunk": "好"}
            yield {"type": "decision", "decision": {"capability": "", "response": "你好"}}

    events = _collect(bridge_mod._plan_and_dispatch(
        FakePlanner(), prompt="hi", ensure_specialists=_ensure_must_not_be_called,
        trace_id="t", history_context="",
    ))
    assert events == [
        {"type": "text", "chunk": "你"},
        {"type": "text", "chunk": "好"},
        {"type": "done", "text": "你好"},
    ]


def test_plan_and_dispatch_non_streaming_planner_prose():
    """A non-streaming planner (mock/stub) still yields the prose + done, and
    spawns nothing."""

    def stub(state):
        return {"capability": "", "response": "  完整答案  "}

    events = _collect(bridge_mod._plan_and_dispatch(
        stub, prompt="hi", ensure_specialists=_ensure_must_not_be_called,
        trace_id="t", history_context="",
    ))
    assert events == [
        {"type": "text", "chunk": "完整答案"},
        {"type": "done", "text": "完整答案"},
    ]


def test_plan_and_dispatch_capability_calls_ensure_specialists():
    """A capability decision must lazily ensure specialists, then dispatch."""
    ensured = {"n": 0}

    async def ensure():
        ensured["n"] += 1
        return ("HOST", "ROUTER", "k")

    captured = {}

    async def fake_dispatch(*, decision, prompt, host, router, hmac_key,
                            trace_id, history_context, delegate=None):
        captured["host"] = host
        captured["router"] = router
        captured["hmac_key"] = hmac_key
        yield {"type": "done", "text": "ok"}

    import web.bridge as wb
    orig = wb.dispatch_decision_stream
    wb.dispatch_decision_stream = fake_dispatch
    try:
        def stub(state):
            return {"capability": "tool.task", "arguments": {"task": "x"}}

        events = _collect(bridge_mod._plan_and_dispatch(
            stub, prompt="do", ensure_specialists=ensure,
            trace_id="t", history_context="",
        ))
    finally:
        wb.dispatch_decision_stream = orig

    assert ensured["n"] == 1
    assert captured["host"] == "HOST" and captured["router"] == "ROUTER"
    assert captured["hmac_key"] == "k"  # dispatch uses the host's own key
    assert events[-1] == {"type": "done", "text": "ok"}


def test_run_turn_streaming_keeps_event_loop_responsive(monkeypatch):
    """A turn does blocking work (planner LLM ``.invoke``, subprocess bootstrap)
    that must NOT run on uvicorn's serving loop -- otherwise the whole server
    freezes for the turn and concurrent requests (switching conversations,
    loading messages) hang. ``run_turn_streaming`` must drive the turn off the
    serving loop and forward events, so the loop keeps ticking throughout."""
    import time

    async def fake_locked(prompt, *, trace_id, session_key, user_id, model_id,
                          base_url="", api_key="", protocol=""):
        time.sleep(0.3)  # stand-in for the turn's blocking work
        yield {"type": "text", "chunk": "hi"}
        yield {"type": "done", "text": "hi"}

    monkeypatch.setattr(bridge_mod, "_run_streaming_locked", fake_locked)

    async def _run():
        ticks = 0

        async def ticker():
            nonlocal ticks
            try:
                while True:
                    ticks += 1
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

        t = asyncio.create_task(ticker())
        await asyncio.sleep(0)  # let the ticker start

        events = []
        async for ev in bridge_mod.run_turn_streaming(
            "hello", session_key="", user_id="", model_id=""
        ):
            events.append(ev)

        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        return events, ticks

    events, ticks = asyncio.run(_run())
    assert {"type": "text", "chunk": "hi"} in events
    assert events[-1] == {"type": "done", "text": "hi"}
    # On-loop: the 0.3s block stops the ticker (~0 ticks). Off-loop: it keeps
    # ticking (~30 in 0.3s).
    assert ticks > 5, f"event loop was blocked during the turn (ticks={ticks})"


def test_prose_turn_spawns_no_specialists(tmp_config_dir, monkeypatch):
    """The core speedup: a prose turn must build the planner from the cached
    catalog and NEVER bootstrap (spawn) specialist subprocesses."""
    import orchestrator.main as om
    import web.bridge as wb

    bootstrap_calls = []

    async def spy_bootstrap(host, router):
        bootstrap_calls.append(1)

    async def fake_catalog():
        return (["tool.task"], {"tool.task": {"description": "d", "inputSchema": {}}})

    def fake_planner(state):
        return {"capability": "", "response": "hello there"}

    monkeypatch.setattr(om, "_bootstrap", spy_bootstrap)
    monkeypatch.setattr(wb, "_capability_catalog", fake_catalog)
    monkeypatch.setattr(wb, "_build_planner", lambda *a, **k: fake_planner)
    monkeypatch.setattr(wb, "_build_planner_context", lambda sk, **k: ("", ""))

    events = _collect(wb._run_streaming_locked(
        "hi", trace_id="t", session_key="", user_id="u", model_id="mock",
    ))

    assert any(e["type"] == "text" for e in events)
    assert events[-1]["type"] == "done"
    assert bootstrap_calls == [], "prose turn must not spawn specialists"


def test_capability_turn_bootstraps_once(tmp_config_dir, monkeypatch):
    """A capability turn lazily bootstraps exactly once, then dispatches."""
    import orchestrator.main as om
    import orchestrator.mcp_host as mh
    import web.bridge as wb

    bootstrap_calls = []

    async def spy_bootstrap(host, router):
        bootstrap_calls.append(1)

    class FakeHost:
        def __init__(self, *a, **k):
            pass

        async def shutdown_all(self):
            pass

    async def fake_catalog():
        return (["tool.task"], {"tool.task": {"description": "d", "inputSchema": {}}})

    def fake_planner(state):
        return {"capability": "tool.task", "arguments": {"task": "x"}}

    async def fake_dispatch(*, decision, prompt, host, router, hmac_key,
                            trace_id, history_context, delegate=None):
        yield {"type": "text", "chunk": "ans"}
        yield {"type": "done", "text": "ans"}

    monkeypatch.setattr(om, "_bootstrap", spy_bootstrap)
    monkeypatch.setattr(mh, "MCPHost", FakeHost)
    monkeypatch.setattr(wb, "_capability_catalog", fake_catalog)
    monkeypatch.setattr(wb, "_build_planner", lambda *a, **k: fake_planner)
    monkeypatch.setattr(wb, "_build_planner_context", lambda sk, **k: ("", ""))
    monkeypatch.setattr(wb, "dispatch_decision_stream", fake_dispatch)

    events = _collect(wb._run_streaming_locked(
        "do it", trace_id="t", session_key="", user_id="u", model_id="mock",
    ))

    assert bootstrap_calls == [1], "capability turn must bootstrap exactly once"
    assert events[-1] == {"type": "done", "text": "ans"}


def test_worker_swallows_turn_cancellation(monkeypatch):
    """When a turn is cancelled mid-stream (client disconnect), the worker
    thread must terminate cleanly — not crash with an unhandled CancelledError
    traceback. Regression for the BaseException-vs-Exception gap in _worker."""
    import threading as _threading

    import web.bridge as wb

    async def fake_locked(prompt, **kw):
        yield {"type": "text", "chunk": "partial"}
        raise asyncio.CancelledError()

    monkeypatch.setattr(wb, "_run_streaming_locked", fake_locked)

    crashes = []
    orig_hook = _threading.excepthook
    _threading.excepthook = lambda args: crashes.append(args)
    try:
        events = _collect(wb.run_turn_streaming(
            "hi", session_key="", user_id="", model_id="",
        ))
    finally:
        _threading.excepthook = orig_hook

    # Partial output was delivered and the stream terminated cleanly...
    assert {"type": "text", "chunk": "partial"} in events
    # ...and the worker thread did NOT crash.
    assert crashes == [], f"worker thread crashed on cancellation: {crashes}"


def test_stream_emits_keepalive_while_waiting(monkeypatch):
    """While the worker is busy (spawn / slow LLM) and no real event has
    arrived, the stream must emit keepalives so a proxy/browser doesn't drop
    the idle connection."""
    import web.bridge as wb

    monkeypatch.setattr(wb, "_HEARTBEAT_SECONDS", 0.05)

    async def slow_locked(prompt, **kw):
        await asyncio.sleep(0.18)  # > heartbeat interval → forces timeouts
        yield {"type": "text", "chunk": "hi"}
        yield {"type": "done", "text": "hi"}

    monkeypatch.setattr(wb, "_run_streaming_locked", slow_locked)

    events = _collect(wb.run_turn_streaming(
        "hi", session_key="", user_id="", model_id="",
    ))

    assert any(e.get("type") == "keepalive" for e in events)
    assert {"type": "text", "chunk": "hi"} in events
    assert events[-1] == {"type": "done", "text": "hi"}


def test_warm_capability_catalog_builds_once(monkeypatch):
    """Startup warm-up builds the catalog exactly once."""
    import web.bridge as wb

    calls = []

    async def fake_cat():
        calls.append(1)
        return (["x"], {})

    monkeypatch.setattr(wb, "_capability_catalog", fake_cat)
    monkeypatch.setattr(wb, "_CATALOG", None)

    asyncio.run(wb.warm_capability_catalog())
    assert calls == [1]


def test_warm_capability_catalog_swallows_errors(monkeypatch):
    """A warm-up failure must NOT crash startup — the first turn builds lazily."""
    import web.bridge as wb

    async def boom():
        raise RuntimeError("spawn fail")

    monkeypatch.setattr(wb, "_capability_catalog", boom)
    monkeypatch.setattr(wb, "_CATALOG", None)

    # Must not raise.
    asyncio.run(wb.warm_capability_catalog())


def test_warm_capability_catalog_seeds_pool_when_enabled(monkeypatch, tmp_config_dir):
    import asyncio as _a

    from web import bridge

    monkeypatch.setenv("WEB_POOL_ENABLED", "1")
    seeded = {"n": 0}

    class _FakeLease:
        host = object(); router = object(); hmac_key = "h"

    class _FakePool:
        async def acquire(self, ctx):
            seeded["n"] += 1
            return _FakeLease()
        async def release(self, lease):
            pass

    monkeypatch.setattr(bridge, "_get_pool", lambda: _FakePool())
    # Avoid the real catalog spawn — only assert the seeding path runs.
    monkeypatch.setattr(bridge, "_capability_catalog",
                        lambda: _a.sleep(0, result=([], {})))

    _a.run(bridge.warm_capability_catalog())
    assert seeded["n"] == 1   # warm-up acquired+released one pooled host


def test_pool_sweeper_calls_sweep_periodically(monkeypatch):
    import asyncio as _a

    from web import bridge

    calls = {"n": 0}

    class _FakePool:
        async def sweep(self):
            calls["n"] += 1

    monkeypatch.setattr(bridge, "_get_pool", lambda: _FakePool())

    async def _drive():
        # interval tiny so the loop ticks a few times fast
        task = _a.create_task(bridge._pool_sweeper(interval=0.01))
        await _a.sleep(0.05)
        task.cancel()
        try:
            await task
        except _a.CancelledError:
            pass

    _a.run(_drive())
    assert calls["n"] >= 2


def test_web_turn_context_custom_endpoint_in_turn_env(tmp_config_dir, monkeypatch):
    for k in ("LANGCHAIN_AGENT_BASE_URL", "LANGCHAIN_AGENT_API_KEY",
              "LANGCHAIN_AGENT_PROTOCOL", "LANGCHAIN_AGENT_MODEL"):
        monkeypatch.delenv(k, raising=False)

    ctx = bridge._web_turn_context(
        user_id="u-alice", model_id="custom/gpt-5.4",
        base_url="https://x.test/v1", api_key="sk-z", protocol="anthropic",
        session_key="s", trace_id="tr",
    )
    env = ctx.turn_env()
    assert env["LANGCHAIN_AGENT_MODEL"] == "custom/gpt-5.4"
    assert env["LANGCHAIN_AGENT_BASE_URL"] == "https://x.test/v1"
    assert env["LANGCHAIN_AGENT_API_KEY"] == "sk-z"
    assert env["LANGCHAIN_AGENT_PROTOCOL"] == "anthropic"

    # Parent env untouched.
    for k in ("LANGCHAIN_AGENT_BASE_URL", "LANGCHAIN_AGENT_API_KEY",
              "LANGCHAIN_AGENT_PROTOCOL", "LANGCHAIN_AGENT_MODEL"):
        assert k not in os.environ


def test_web_turn_context_no_endpoint_omits_custom_vars(tmp_config_dir, monkeypatch):
    for k in ("LANGCHAIN_AGENT_BASE_URL", "LANGCHAIN_AGENT_API_KEY",
              "LANGCHAIN_AGENT_PROTOCOL"):
        monkeypatch.delenv(k, raising=False)
    ctx = bridge._web_turn_context(user_id="u-bob", model_id="openai/gpt-4o",
                                   session_key="s", trace_id="tr")
    env = ctx.turn_env()
    # Empty optionals are omitted (not set to "") so they don't clobber a child default.
    assert "LANGCHAIN_AGENT_BASE_URL" not in env
    assert "LANGCHAIN_AGENT_API_KEY" not in env
    assert "LANGCHAIN_AGENT_PROTOCOL" not in env
