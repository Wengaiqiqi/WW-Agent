from __future__ import annotations

import os
from pathlib import Path

from web import bridge


def test_web_turn_env_sets_and_restores(tmp_config_dir, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_AGENT_PERMISSION_MODE", "read-only")
    monkeypatch.delenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_MODEL", raising=False)

    with bridge._web_turn_env(user_id="u-alice", model_id="anthropic/claude-opus-4-7") as ws:
        assert os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] == "workspace-write"
        assert os.environ["LANGCHAIN_AGENT_MEMORY_USER"] == "u-alice"
        assert os.environ["LANGCHAIN_AGENT_MODEL"] == "anthropic/claude-opus-4-7"
        # per-user workspace under the config dir, and it exists
        assert "u-alice" in os.environ["LANGCHAIN_AGENT_WORKSPACE_ROOT"]
        assert Path(os.environ["LANGCHAIN_AGENT_WORKSPACE_ROOT"]).is_dir()
        assert Path(ws) == Path(os.environ["LANGCHAIN_AGENT_WORKSPACE_ROOT"])

    # restored to the pre-existing value, model var removed (was unset before)
    assert os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] == "read-only"
    assert "LANGCHAIN_AGENT_WORKSPACE_ROOT" not in os.environ
    assert "LANGCHAIN_AGENT_MEMORY_USER" not in os.environ
    assert "LANGCHAIN_AGENT_MODEL" not in os.environ


def test_web_turn_env_two_users_isolated(tmp_config_dir):
    with bridge._web_turn_env(user_id="u-alice", model_id="") as ws_a:
        pass
    with bridge._web_turn_env(user_id="u-bob", model_id="") as ws_b:
        pass
    assert Path(ws_a) != Path(ws_b)
    assert "u-alice" in str(ws_a) and "u-bob" in str(ws_b)


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
