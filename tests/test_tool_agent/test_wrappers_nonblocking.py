"""Tool wrappers must not block the asyncio event loop.

Before this fix, ``_wrap_run_python`` / ``_wrap_run_command`` / the
``_wrap_web_*`` family called sync functions directly from ``async def``
coroutines. While a blocking call ran (up to 180s for shell, 30s for
HTTP), the entire tool-agent event loop was frozen — orchestrator SSE
heartbeats stalled, the ``clarify`` reverse-channel queued silently, and
the user's spinner went mute.

Each wrapper was rewritten to offload its blocking core into
``asyncio.to_thread``. These tests prove the loop keeps making progress
while the inner work is busy.

Strategy: monkey-patch the *inner* sync function with one that
``time.sleep``s. Start the wrapper as a task, and concurrently run a
``asyncio.sleep(0)`` loop counter. If the wrapper still blocked the
event loop, the counter would not advance until the wrapper returned;
with ``to_thread`` it advances continuously.
"""
from __future__ import annotations

import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_run_python_does_not_block_event_loop(monkeypatch):
    import agents.tool_agent.tool_executor as te

    # Replace the blocking core with one that just sleeps. We don't care
    # what it returns — only that the wrapper offloads it.
    def fake_run_python(code, timeout):
        time.sleep(0.5)
        return "fake-result"

    monkeypatch.setattr(
        "tool.tool_shell.run_python_code",
        fake_run_python,
    )

    counter = [0]

    async def _tick():
        # If the event loop is alive, this coroutine increments the counter
        # ~once per millisecond. If the loop is frozen during the 0.5s
        # sleep, the counter stays at 0.
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            counter[0] += 1
            await asyncio.sleep(0)

    # ``_wrap_run_python`` writes a log file — point it somewhere harmless.
    monkeypatch.setattr(te, "_rotate_runpython_log", lambda p: None)

    wrapper_task = asyncio.create_task(te._wrap_run_python({"code": "irrelevant"}))
    ticker_task = asyncio.create_task(_tick())

    result, _ = await asyncio.gather(wrapper_task, ticker_task)

    assert result == "fake-result"
    # If the wrapper had blocked, counter would be 0 (or 1, the pre-await
    # increment). With to_thread, we expect thousands of ticks in 500ms.
    # A conservative threshold of 100 still proves the loop was alive.
    assert counter[0] > 100, (
        f"event loop appears to have been blocked: only {counter[0]} ticks "
        f"during the wrapped sync work — to_thread offload may have regressed."
    )


@pytest.mark.asyncio
async def test_run_command_does_not_block_event_loop(monkeypatch):
    import agents.tool_agent.tool_executor as te  # noqa: F401 — for monkeypatch root

    def fake_run_shell(command, timeout):
        time.sleep(0.5)
        return "fake-result"

    monkeypatch.setattr("tool.tool_shell.run_shell_command", fake_run_shell)

    counter = [0]

    async def _tick():
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            counter[0] += 1
            await asyncio.sleep(0)

    from agents.tool_agent.tool_executor import _wrap_run_command
    wrapper = asyncio.create_task(_wrap_run_command({"command": "ignored"}))
    ticker = asyncio.create_task(_tick())
    result, _ = await asyncio.gather(wrapper, ticker)

    assert result == "fake-result"
    assert counter[0] > 100, (
        f"_wrap_run_command blocked the loop: {counter[0]} ticks"
    )


@pytest.mark.asyncio
async def test_web_extract_does_not_block_event_loop(monkeypatch):
    def fake_web_extract(url, max_chars):
        time.sleep(0.3)
        return {"text": "hello", "url": url}

    monkeypatch.setattr("tool.tool_web.web_extract", fake_web_extract)

    counter = [0]

    async def _tick():
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            counter[0] += 1
            await asyncio.sleep(0)

    from agents.tool_agent.tool_executor import _wrap_web_extract
    wrapper = asyncio.create_task(
        _wrap_web_extract({"url": "https://example.com"})
    )
    ticker = asyncio.create_task(_tick())
    result, _ = await asyncio.gather(wrapper, ticker)

    assert "hello" in str(result)
    assert counter[0] > 100, (
        f"_wrap_web_extract blocked the loop: {counter[0]} ticks"
    )
