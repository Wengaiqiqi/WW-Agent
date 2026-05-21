"""A2A streaming dispatch authz.

Pins the security fix: the orchestrator now mints an authz_grant for
``tool.task`` and tool-agent's ``a2a_stream_dispatch`` verifies it. The
previous behavior — no grant, no verification — let prompt-injected
``tool.task`` payloads run under whatever permission the agent process had,
silently bypassing the user's selected mode.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
import jwt as pyjwt
import pytest

from agents.shared.a2a_server import A2AServer, A2AHandler, A2AStreamHandler


HMAC_KEY = "authz-test-key-fixed"


def _grant(*, allowed_tools, mode="workspace-write", expired=False, key=HMAC_KEY):
    return pyjwt.encode(
        {
            "iss": "orchestrator",
            "sub": "tool-agent",
            "exp": int(time.time()) + (-1 if expired else 60),
            "permission_mode": mode,
            "allowed_tools": list(allowed_tools),
            "trace_id": "test",
        },
        key,
        algorithm="HS256",
    )


async def _post_stream(url: str, *, task: str, meta: dict) -> list[dict]:
    """Drive ``/a2a/stream`` and return the parsed SSE events."""
    events: list[dict] = []
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        async with client.stream(
            "POST", f"{url}/a2a/stream",
            json={
                "jsonrpc": "2.0", "id": "test",
                "method": "tasks/sendStream",
                "params": {"task": task, "_meta": meta},
            },
        ) as resp:
            resp.raise_for_status()
            buf = ""
            async for chunk in resp.aiter_bytes():
                buf += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buf:
                    line, buf = buf.split("\n\n", 1)
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
    return events


async def _start_server_with_real_dispatch(monkeypatch):
    """Spin up an A2AServer that mounts the REAL ``handle_tool_task_stream``
    from ``agents.tool_agent.main``. We stub out the heavyweight LLM / tool
    construction so the test stays in-process and fast, but the auth /
    grant / mode-propagation logic under test is the production code path.
    """
    import agents.tool_agent.main as tool_agent_main

    captured: dict[str, Any] = {}

    class _FakeAgent:
        async def run(self, *, task):
            yield {"type": "done", "text": "ok"}

    def _fake_tool_agent_loop(*, llm, tools, context="", **_kw):
        return _FakeAgent()

    async def _fake_get_llm():
        return object()

    def _fake_make_tools(mode="danger-full-access"):
        captured["mode"] = mode
        return []

    monkeypatch.setattr(tool_agent_main, "ToolAgentLoop", _fake_tool_agent_loop)
    monkeypatch.setattr(tool_agent_main, "get_llm", _fake_get_llm)
    monkeypatch.setattr(tool_agent_main, "make_langchain_tools", _fake_make_tools)
    monkeypatch.setenv("AUTHZ_HMAC_KEY", HMAC_KEY)

    async def rpc_dispatch(skill_id, inp, meta):
        return {"error": f"unused in this test ({skill_id!r})"}

    server = A2AServer(
        handler=A2AHandler(handler=rpc_dispatch),
        stream_handler=A2AStreamHandler(handler=tool_agent_main.handle_tool_task_stream),
    )
    await server.start()
    return server, captured


@pytest.mark.asyncio
async def test_tool_task_without_grant_is_refused(monkeypatch):
    """No authz_grant in meta → tool-agent must refuse, not silently elevate."""
    server, _captured = await _start_server_with_real_dispatch(monkeypatch)
    try:
        events = await _post_stream(server.base_url, task="do something", meta={})
        assert any(
            e.get("type") == "error" and "authz" in e.get("message", "").lower()
            or e.get("type") == "error" and "no authz_grant" in e.get("message", "")
            for e in events
        ), f"expected an authz-error event, got: {events}"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_tool_task_with_expired_grant_is_refused(monkeypatch):
    server, _captured = await _start_server_with_real_dispatch(monkeypatch)
    try:
        grant = _grant(allowed_tools=["tool.task"], expired=True)
        events = await _post_stream(
            server.base_url, task="do something",
            meta={"authz_grant": grant},
        )
        msgs = [e.get("message", "").lower() for e in events if e.get("type") == "error"]
        assert any("expired" in m for m in msgs), f"expected expired error, got: {events}"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_tool_task_with_wrong_tool_grant_is_refused(monkeypatch):
    """A grant minted for ``read_file`` must not authorize ``tool.task``."""
    server, _captured = await _start_server_with_real_dispatch(monkeypatch)
    try:
        grant = _grant(allowed_tools=["read_file"])  # wrong tool
        events = await _post_stream(
            server.base_url, task="do something",
            meta={"authz_grant": grant},
        )
        msgs = [e.get("message", "").lower() for e in events if e.get("type") == "error"]
        assert any(
            "allowed_tools" in m or "authz" in m or "not in" in m
            for m in msgs
        ), f"expected allowed_tools error, got: {events}"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_tool_task_grant_permission_mode_propagates(monkeypatch):
    """A valid read-only grant must result in tool-agent constructing the
    LangChain tool set under read-only — i.e. ``make_langchain_tools`` is
    called with ``mode="read-only"``."""
    server, captured = await _start_server_with_real_dispatch(monkeypatch)
    try:
        grant = _grant(allowed_tools=["tool.task"], mode="read-only")
        events = await _post_stream(
            server.base_url, task="read README",
            meta={"authz_grant": grant},
        )
        assert captured.get("mode") == "read-only", (
            f"mode did not propagate: captured={captured}, events={events}"
        )
    finally:
        await server.stop()


def test_rpc_path_refuses_tool_task():
    """The non-streaming RPC endpoint (/a2a) used to handle
    ``skill_id="tool.task"`` by spinning up a ReAct loop with the full
    toolset — no grant verification, no mode gating. The branch was dead
    code (no production caller) but it was a latent escape hatch.

    After the fix, hitting ``tool.task`` via /a2a returns an explanatory
    error pointing the caller at /a2a/stream. ``a2a_dispatch`` is a closure
    inside ``amain`` and not importable, so we assert on the source: the
    refusal message must be present AND the previous bypass
    (``_run_agent_nonstreaming``) must be gone.
    """
    import agents.tool_agent.main as tool_agent_main
    from pathlib import Path

    src = Path(tool_agent_main.__file__).read_text(encoding="utf-8")
    refusal = "tool.task is only available via the streaming endpoint"
    assert refusal in src, (
        f"RPC refusal message missing from main.py — the dead-code "
        f"bypass may have been re-introduced. Looked for: {refusal!r}"
    )
    # And the actual offending function must be gone (not just commented out).
    assert "async def _run_agent_nonstreaming" not in src, (
        "_run_agent_nonstreaming was removed because it defaulted to "
        "danger-full-access; re-adding it without grant gating is a "
        "permission-mode bypass."
    )


@pytest.mark.asyncio
async def test_tool_task_unknown_mode_in_grant_is_refused(monkeypatch):
    """A grant whose ``permission_mode`` claim is outside the whitelist must
    be refused. Without this check, an unknown mode silently degrades to
    "no tools bound" — the user sees a mute agent instead of a clear error."""
    server, _captured = await _start_server_with_real_dispatch(monkeypatch)
    try:
        # Hand-mint a grant with a bogus mode — the real PermissionGate
        # would refuse to do this, but a tampered/forged token could.
        grant = _grant(allowed_tools=["tool.task"], mode="hacker-mode")
        events = await _post_stream(
            server.base_url, task="anything",
            meta={"authz_grant": grant},
        )
        msgs = [e.get("message", "").lower() for e in events if e.get("type") == "error"]
        assert any("unknown permission_mode" in m for m in msgs), (
            f"expected unknown-mode error, got: {events}"
        )
    finally:
        await server.stop()
