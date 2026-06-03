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
    monkeypatch.delenv("GATEWAY_MAX_CONCURRENCY", raising=False)
    importlib.reload(runner)


import asyncio
import os

import pytest


@pytest.mark.asyncio
async def test_run_turn_does_not_mutate_process_env(monkeypatch):
    """A gateway turn resolves planner cfg + memory user from its TurnContext,
    not process-global env — so concurrent turns stay isolated."""
    from gateway import runner

    monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_MODEL", raising=False)

    captured = {}

    async def fake_bootstrap(host, router):
        return None

    def fake_build_planner(router, *, context_text="", cfg=None):
        captured["cfg"] = cfg
        return lambda state: {"capability": "", "response": "ok"}

    def fake_planner_context(session_key, *, memory_user=""):
        captured["memory_user"] = memory_user
        return "", ""

    monkeypatch.setattr(runner, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(runner, "_build_planner", fake_build_planner)
    monkeypatch.setattr(runner, "_build_planner_context", fake_planner_context)

    class _FakeHost:
        def __init__(self, **k): pass
        async def shutdown_all(self): pass

    monkeypatch.setattr(runner, "MCPHost", _FakeHost)

    reply = await runner.run_turn("hello", session_key="s", user_id="alice")
    assert reply == "ok"
    # The per-turn memory user reached the snapshot via ctx, NOT os.environ.
    assert captured["memory_user"] == "alice"
    assert captured["cfg"] is not None          # planner cfg came from ctx
    assert "LANGCHAIN_AGENT_MEMORY_USER" not in os.environ
    assert "LANGCHAIN_AGENT_MODEL" not in os.environ


def test_feishu_ws_dispatch_semaphore_sized_from_flag(monkeypatch):
    import importlib

    monkeypatch.setenv("GATEWAY_MAX_CONCURRENCY", "3")
    from gateway import runner, feishu_ws
    importlib.reload(runner)
    importlib.reload(feishu_ws)
    # BoundedSemaphore admits exactly N concurrent holders.
    got = [feishu_ws._dispatch_sem.acquire(blocking=False) for _ in range(3)]
    assert all(got)
    assert feishu_ws._dispatch_sem.acquire(blocking=False) is False  # 4th blocked
    for _ in got:
        feishu_ws._dispatch_sem.release()
    monkeypatch.delenv("GATEWAY_MAX_CONCURRENCY", raising=False)
    importlib.reload(runner)
    importlib.reload(feishu_ws)


@pytest.mark.asyncio
async def test_two_turns_overlap_when_limit_raised(monkeypatch):
    import importlib

    monkeypatch.setenv("GATEWAY_MAX_CONCURRENCY", "2")
    from gateway import runner
    importlib.reload(runner)
    try:
        both_in = asyncio.Semaphore(0)

        async def _wait_two(sem):
            await sem.acquire()
            await sem.acquire()
            sem.release()
            sem.release()

        async def fake_locked(prompt, **kw):
            both_in.release()
            # Only completes once BOTH turns have entered — proves overlap (would
            # time out under the old single-lock / semaphore(1)).
            await asyncio.wait_for(_wait_two(both_in), timeout=2.0)
            return prompt

        monkeypatch.setattr(runner, "_run_turn_locked", fake_locked)
        results = await asyncio.gather(
            runner.run_turn("a", session_key="a"),
            runner.run_turn("b", session_key="b"),
        )
        assert set(results) == {"a", "b"}
    finally:
        monkeypatch.delenv("GATEWAY_MAX_CONCURRENCY", raising=False)
        importlib.reload(runner)


def test_feishu_set_dispatch_limit_rebinds(monkeypatch):
    import importlib
    monkeypatch.delenv("GATEWAY_MAX_CONCURRENCY", raising=False)
    from gateway import runner, feishu_ws
    importlib.reload(runner)
    importlib.reload(feishu_ws)
    # default is 1 -> exactly one holder
    assert feishu_ws._dispatch_sem.acquire(blocking=False) is True
    assert feishu_ws._dispatch_sem.acquire(blocking=False) is False
    feishu_ws._dispatch_sem.release()

    feishu_ws.set_dispatch_limit(3)
    got = [feishu_ws._dispatch_sem.acquire(blocking=False) for _ in range(3)]
    assert all(got)
    assert feishu_ws._dispatch_sem.acquire(blocking=False) is False
    for _ in got:
        feishu_ws._dispatch_sem.release()

    feishu_ws.set_dispatch_limit(0)  # floored to 1
    assert feishu_ws._dispatch_sem.acquire(blocking=False) is True
    assert feishu_ws._dispatch_sem.acquire(blocking=False) is False
    feishu_ws._dispatch_sem.release()

    importlib.reload(feishu_ws)
