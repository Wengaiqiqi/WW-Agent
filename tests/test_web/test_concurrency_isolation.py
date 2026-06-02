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

    both_in = asyncio.Semaphore(0)

    async def _wait_two(sem):
        await sem.acquire()
        await sem.acquire()
        sem.release()
        sem.release()

    async def fake_locked(prompt, **kw):
        both_in.release()
        # Wait until the other turn has also entered — proves concurrency.
        await asyncio.wait_for(_wait_two(both_in), timeout=2.0)
        yield {"type": "done", "text": prompt}

    monkeypatch.setattr(bridge, "_stream_off_loop",
                        lambda *a, **k: fake_locked(*a, **k))

    async def drain(p):
        return [e async for e in bridge.run_turn_streaming(p, session_key=p)]

    results = await asyncio.gather(drain("a"), drain("b"))
    assert {r[-1]["text"] for r in results} == {"a", "b"}
