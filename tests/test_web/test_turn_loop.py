from __future__ import annotations

import asyncio

import pytest

from web.turn_loop import TurnLoop


@pytest.mark.asyncio
async def test_run_coroutine_executes_on_the_loop_thread():
    loop = TurnLoop()
    loop.start()
    try:
        async def work():
            # The coroutine runs on the turn loop, NOT the serving loop.
            return id(asyncio.get_running_loop())

        fut = loop.run_coroutine(work())
        result = await asyncio.wrap_future(fut)
        assert result == loop.loop_id
        assert result != id(asyncio.get_running_loop())  # different loop
    finally:
        loop.stop()


@pytest.mark.asyncio
async def test_run_in_loop_factory_runs_an_async_callable():
    loop = TurnLoop()
    loop.start()
    try:
        seen = {}

        async def make():
            seen["loop"] = id(asyncio.get_running_loop())
            return 42

        fut = loop.run_coroutine_factory(make)
        assert await asyncio.wrap_future(fut) == 42
        assert seen["loop"] == loop.loop_id
    finally:
        loop.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_joins_thread():
    loop = TurnLoop()
    loop.start()
    loop.stop()
    loop.stop()  # second stop must not raise
    assert not loop.is_running


@pytest.mark.asyncio
async def test_restart_after_stop_runs_on_a_fresh_loop():
    loop = TurnLoop()
    loop.start()
    loop.stop()

    loop.start()  # restart: must install a NEW, running loop (not the stale one)
    try:
        assert loop.is_running

        async def work():
            return id(asyncio.get_running_loop())

        # Work scheduled after restart must actually run on the new loop — this
        # would hang/time out if start() returned before the fresh loop was up
        # and the coroutine were scheduled on the previous, closed loop.
        result = await asyncio.wait_for(
            asyncio.wrap_future(loop.run_coroutine(work())), timeout=2.0,
        )
        assert result == loop.loop_id
    finally:
        loop.stop()
