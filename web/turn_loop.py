"""A single persistent event loop on a dedicated daemon thread.

Pooled ``MCPHost``s hold asyncio stdio transports bound to the loop that created
them, so every turn that touches a pooled host must run on ONE long-lived loop
rather than today's fresh-``asyncio.run``-per-thread. The serving loop (uvicorn)
hands work to this loop with :meth:`run_coroutine` (which returns a
``concurrent.futures.Future`` the caller can ``asyncio.wrap_future``) and streams
events back over a thread-safe queue (see ``web.bridge``).
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Awaitable, Callable, Coroutine


class TurnLoop:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="web-turn-loop", daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    @property
    def is_running(self) -> bool:
        return self._loop is not None and self._loop.is_running()

    @property
    def loop_id(self) -> int:
        assert self._loop is not None, "TurnLoop not started"
        return id(self._loop)

    def run_coroutine(self, coro: Coroutine[Any, Any, Any]) -> Future:
        """Schedule an already-created coroutine on the turn loop."""
        assert self._loop is not None, "TurnLoop not started"
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run_coroutine_factory(
        self, make: Callable[[], Awaitable[Any]],
    ) -> Future:
        """Schedule ``make()`` so the coroutine is CREATED on the turn loop.

        Use this when the coroutine (or what it awaits) is loop-affine and must
        not be instantiated on the serving loop."""
        assert self._loop is not None, "TurnLoop not started"

        async def _wrap() -> Any:
            return await make()

        return asyncio.run_coroutine_threadsafe(_wrap(), self._loop)

    def call_soon(self, fn: Callable[..., Any], *args: Any) -> None:
        """Schedule a plain callable on the turn loop (e.g. task.cancel)."""
        assert self._loop is not None, "TurnLoop not started"
        self._loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        if self._loop is None or self._thread is None:
            return
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10.0)
        self._thread = None
