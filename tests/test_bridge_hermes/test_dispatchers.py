"""Tests for ACP→A2A dispatcher translation, using a fake ACP client."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from bridge.hermes_a2a.dispatchers import make_dispatchers


class FakeACP:
    """Stand-in for HermesACPClient with deterministic, in-memory behavior."""

    def __init__(self):
        self._counter = 0
        self.known: set[str] = set()
        self.prompts: list[tuple[str, str]] = []

    async def ensure_session(self, context_id):
        if context_id and context_id in self.known:
            return context_id
        self._counter += 1
        sid = f"s{self._counter}"
        self.known.add(sid)
        return sid

    async def run_prompt(self, session_id, text) -> AsyncIterator[dict]:
        self.prompts.append((session_id, text))
        yield {"type": "text", "text": f"echo: {text}"}
        yield {"type": "task", "state": "completed", "result": f"echo: {text}"}

    async def prompt_collect(self, session_id, text) -> str:
        self.prompts.append((session_id, text))
        return f"echo: {text}"

    def status(self) -> dict:
        return {"state": "idle", "current_task": None, "sessions": len(self.known)}


def _params(text, context_id=None):
    p = {"message": {"role": "user", "parts": [{"text": text}]}}
    if context_id is not None:
        p["context_id"] = context_id
    return p


@pytest.mark.asyncio
async def test_stream_dispatcher_delegate_emits_working_then_completed():
    acp = FakeACP()
    _skill, stream = make_dispatchers(acp)
    events = [
        ev async for ev in stream("message/stream", _params("do thing"), {"peer_id": "caller"})
    ]
    assert events[0] == {"type": "task", "state": "working", "message": "delegating to hermes"}
    assert {"type": "text", "text": "echo: do thing"} in events
    completed = [e for e in events if e.get("state") == "completed"]
    assert completed and completed[0]["result"] == "echo: do thing"


@pytest.mark.asyncio
async def test_stream_dispatcher_rejects_disallowed_caller():
    acp = FakeACP()
    _skill, stream = make_dispatchers(acp, allowed_peer="trusted")
    events = [
        ev async for ev in stream("message/stream", _params("x"), {"peer_id": "intruder"})
    ]
    assert events == [{"type": "task", "state": "failed", "error": "caller peer not allowed"}]
    assert acp.prompts == []


@pytest.mark.asyncio
async def test_skill_dispatcher_chat_first_turn_allocates_context():
    acp = FakeACP()
    skill, _stream = make_dispatchers(acp)
    out = await skill("message/send", _params("hi"), {"peer_id": "caller"})
    assert out["reply"] == "echo: hi"
    assert out["context_id"] == "s1"


@pytest.mark.asyncio
async def test_skill_dispatcher_chat_reuses_context():
    acp = FakeACP()
    skill, _stream = make_dispatchers(acp)
    first = await skill("message/send", _params("hi"), {"peer_id": "caller"})
    second = await skill("message/send",
                         _params("again", context_id=first["context_id"]),
                         {"peer_id": "caller"})
    assert second["context_id"] == first["context_id"]   # same ACP session
    assert [sid for sid, _ in acp.prompts] == ["s1", "s1"]


@pytest.mark.asyncio
async def test_skill_dispatcher_status():
    acp = FakeACP()
    skill, _stream = make_dispatchers(acp)
    out = await skill("status/query", {}, {"peer_id": "caller"})
    assert out["state"] == "idle"


@pytest.mark.asyncio
async def test_skill_dispatcher_unsupported_method():
    acp = FakeACP()
    skill, _stream = make_dispatchers(acp)
    out = await skill("message/bogus", _params("x"), {"peer_id": "caller"})
    assert "unsupported" in out["error"]
