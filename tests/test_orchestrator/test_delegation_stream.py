from __future__ import annotations

import pytest

from orchestrator.delegation import delegate_via_a2a, delegate_via_a2a_stream


def _fake_delegate(events):
    async def _gen(*, peer_id, task, meta, context=""):
        _gen.captured = {"peer_id": peer_id, "task": task, "meta": meta, "context": context}
        for ev in events:
            yield ev
    return _gen


@pytest.mark.asyncio
async def test_stream_forwards_events_and_signs_tool_grant():
    events = [
        {"type": "thinking", "text": "hmm"},
        {"type": "text", "chunk": "hello "},
        {"type": "text", "chunk": "world"},
        {"type": "done", "text": "hello world"},
    ]
    delegate = _fake_delegate(events)
    seen = []
    async for ev in delegate_via_a2a_stream(
        capability="tool.task",
        arguments={"task": "do it"},
        user_input="ignored when arguments.task present",
        hmac_key="k",
        trace_id="t1",
        permission_mode="workspace-write",
        history_context="ctx",
        delegate=delegate,
    ):
        seen.append(ev)
    assert seen == events
    assert delegate.captured["peer_id"] == "tool-agent"
    assert delegate.captured["task"] == "do it"
    assert delegate.captured["context"] == "ctx"
    assert delegate.captured["meta"]["authz_grant"]  # a grant was minted


@pytest.mark.asyncio
async def test_non_stream_wrapper_still_returns_final_text():
    events = [
        {"type": "text", "chunk": "par"},
        {"type": "text", "chunk": "tial"},
        {"type": "done", "text": ""},  # done with no text -> fall back to buffer
    ]
    out = await delegate_via_a2a(
        capability="tool.task",
        arguments={},
        user_input="hi",
        hmac_key="k",
        trace_id="t1",
        permission_mode="workspace-write",
        delegate=_fake_delegate(events),
    )
    assert out == "partial"


@pytest.mark.asyncio
async def test_error_event_raises_in_wrapper():
    out_events = [{"type": "error", "message": "boom"}]
    with pytest.raises(RuntimeError, match="boom"):
        await delegate_via_a2a(
            capability="tool.task",
            arguments={},
            user_input="hi",
            hmac_key="k",
            trace_id="t1",
            permission_mode="workspace-write",
            delegate=_fake_delegate(out_events),
        )
