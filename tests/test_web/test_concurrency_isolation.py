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

    from web import bridge
    from web import config as web_config

    # Reloading web.bridge swaps the module's process-wide TurnLoop/pool out from
    # under any running instance. Stop the current shared loop first so its daemon
    # thread isn't orphaned (which otherwise prints asyncio teardown noise at
    # interpreter exit), then reload to pick up the raised WEB_MAX_CONCURRENCY.
    bridge._TURN_LOOP.stop()
    importlib.reload(web_config)
    importlib.reload(bridge)
    try:
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
    finally:
        # Restore pristine module state for later tests: stop this module's loop,
        # drop the env override, and reload so the semaphore resets to the
        # default (1) and the turn loop / pool are fresh.
        bridge._TURN_LOOP.stop()
        monkeypatch.delenv("WEB_MAX_CONCURRENCY", raising=False)
        importlib.reload(web_config)
        importlib.reload(bridge)
