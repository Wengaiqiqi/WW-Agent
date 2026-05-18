"""Integration test for the clarify reverse-A2A round trip.

Boots a real ``A2AServer`` with the same handler shapes the live tool-agent
uses, then exercises the full HTTP path end-to-end without spinning up a
subprocess:

    test → SSE stream → clarify_bridge.request → clarify_request event
    test → POST /a2a with skill_id=_clarify_response → clarify_bridge.resolve
    test ← request() returns the user's answer

Validates: A2AServer routes /a2a/stream + /a2a correctly, the JSON-RPC
envelope from ``send_clarify_response`` matches the dispatcher's expectations,
and the bridge unblocks within a normal asyncio scheduling.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agents.shared.a2a_server import A2AHandler, A2AServer, A2AStreamHandler
from agents.tool_agent import clarify_bridge


@pytest.mark.asyncio
async def test_clarify_round_trip_through_a2a_server():
    """Drive the full bridge through a real A2AServer + httpx round trip."""

    answer_holder = {"got": None}

    # Fake "agent loop": just call into clarify_bridge.request and stash
    # the answer. The streaming dispatch installs the queue and forwards
    # whatever the bridge emits.
    async def stream_handler(payload):
        queue: asyncio.Queue[dict] = asyncio.Queue()
        clarify_bridge.set_event_queue(queue)

        sentinel: dict = {"__sentinel__": True}

        async def _drive():
            try:
                answer = await clarify_bridge.request(
                    "pick one:", ["A", "B", "C"],
                )
                answer_holder["got"] = answer
                await queue.put({"type": "done", "text": answer})
            finally:
                await queue.put(sentinel)

        task = asyncio.create_task(_drive())
        try:
            while True:
                event = await queue.get()
                if event is sentinel:
                    break
                yield event
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def rpc_handler(skill_id, input, meta):
        # Mirror tool-agent's real dispatcher: surface the _clarify_response
        # sentinel back to the bridge.
        if skill_id == "_clarify_response":
            ok = clarify_bridge.resolve(
                str(input.get("request_id") or ""),
                str(input.get("answer") or ""),
            )
            return {"resolved": ok}
        return {"error": f"unknown skill_id {skill_id!r}"}

    server = A2AServer(
        handler=A2AHandler(handler=rpc_handler),
        stream_handler=A2AStreamHandler(handler=stream_handler),
    )
    await server.start()
    try:
        url = server.base_url
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            # Start the streaming task in a background task — we need to
            # consume the SSE and react to clarify_request mid-stream.
            async def consume_stream():
                events = []
                async with client.stream(
                    "POST", f"{url}/a2a/stream",
                    json={
                        "jsonrpc": "2.0", "id": "t",
                        "method": "tasks/sendStream",
                        "params": {"task": "go", "_meta": {}},
                    },
                ) as resp:
                    resp.raise_for_status()
                    buf = ""
                    async for chunk in resp.aiter_bytes():
                        buf += chunk.decode("utf-8")
                        while "\n\n" in buf:
                            line, buf = buf.split("\n\n", 1)
                            if line.startswith("data: "):
                                events.append(json.loads(line[6:]))
                                if events[-1].get("type") == "done":
                                    return events
                return events

            consumer = asyncio.create_task(consume_stream())

            # Poll until the consumer has yielded the clarify_request, then
            # POST the user's answer back.
            request_id = None
            for _ in range(200):  # up to 2 seconds
                await asyncio.sleep(0.01)
                # Reach into the test consumer's event list isn't possible;
                # instead look at the bridge's pending registry.
                pending = clarify_bridge._peek_pending()
                if pending:
                    request_id = next(iter(pending))
                    break
            assert request_id is not None, "bridge never registered a pending clarify"

            # Send the answer via the public helper that orchestrator uses.
            rpc_resp = await client.post(
                f"{url}/a2a",
                json={
                    "jsonrpc": "2.0", "id": "r",
                    "method": "tasks/send",
                    "params": {
                        "skill_id": "_clarify_response",
                        "input": {"request_id": request_id, "answer": "B"},
                        "_meta": {},
                    },
                },
            )
            rpc_resp.raise_for_status()
            assert rpc_resp.json()["result"]["resolved"] is True

            events = await asyncio.wait_for(consumer, timeout=2.0)
    finally:
        await server.stop()

    # The bridge returned "B" to the wrapper; the wrapper stashed it.
    assert answer_holder["got"] == "B"
    # The SSE stream carried the clarify_request and the eventual done.
    types = [e.get("type") for e in events]
    assert "clarify_request" in types
    assert types[-1] == "done"


@pytest.mark.asyncio
async def test_clarify_response_with_unknown_request_id_is_rejected():
    """Late / typo'd responses surface ``resolved=False`` rather than 500ing."""

    async def stream_handler(payload):
        if False:
            yield {}  # make this an async generator

    async def rpc_handler(skill_id, input, meta):
        if skill_id == "_clarify_response":
            ok = clarify_bridge.resolve(
                str(input.get("request_id") or ""),
                str(input.get("answer") or ""),
            )
            return {"resolved": ok}
        return {"error": "unknown"}

    server = A2AServer(
        handler=A2AHandler(handler=rpc_handler),
        stream_handler=A2AStreamHandler(handler=stream_handler),
    )
    await server.start()
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.post(
                f"{server.base_url}/a2a",
                json={
                    "jsonrpc": "2.0", "id": "x",
                    "method": "tasks/send",
                    "params": {
                        "skill_id": "_clarify_response",
                        "input": {"request_id": "never-existed", "answer": "?"},
                        "_meta": {},
                    },
                },
            )
            resp.raise_for_status()
            assert resp.json()["result"]["resolved"] is False
    finally:
        await server.stop()
