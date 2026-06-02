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
