from __future__ import annotations
import asyncio
import json
import socket
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable
import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import StreamingResponse


HandlerFunc = Callable[[str, dict, dict], Awaitable[dict]]
StreamHandlerFunc = Callable[[dict], AsyncIterator[dict[str, Any]]]


@dataclass
class A2AHandler:
    handler: HandlerFunc

    async def dispatch(self, payload: dict) -> dict:
        params = payload.get("params") or {}
        skill_id = params.get("skill_id")
        inp = params.get("input") or {}
        meta = params.get("_meta") or {}
        result = await self.handler(skill_id, inp, meta)
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}


@dataclass
class A2AStreamHandler:
    handler: StreamHandlerFunc


def _pick_free_port() -> int:
    """Bind a socket to port 0, get the assigned port, then close the socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _sse_generator(handler, params: dict) -> AsyncIterator[str]:
    """Yield SSE-formatted lines from an async event iterator."""
    async for event in handler(params):
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class A2AServer:
    def __init__(
        self, *,
        handler: A2AHandler,
        host: str = "127.0.0.1", port: int = 0,
        stream_handler: A2AStreamHandler | None = None,
    ):
        self._handler = handler
        self._stream_handler = stream_handler
        self._host = host
        # Pre-select a free port so we know the URL before uvicorn starts.
        self._port = port if port != 0 else _pick_free_port()
        self._app = FastAPI()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

        @self._app.post("/a2a")
        async def _endpoint(req: Request):
            payload = await req.json()
            return await self._handler.dispatch(payload)

        if self._stream_handler is not None:
            @self._app.post("/a2a/stream")
            async def _stream_endpoint(req: Request):
                payload = await req.json()
                params = payload.get("params") or {}
                task_payload = {
                    "task": params.get("task") or params.get("input", {}).get("task", ""),
                    "context": params.get("context", ""),
                    "meta": params.get("_meta") or params.get("meta", {}),
                }
                return StreamingResponse(
                    _sse_generator(self._stream_handler.handler, task_payload),
                    media_type="text/event-stream",
                )

    async def start(self) -> None:
        # _pick_free_port has a small TOCTOU window: between the probe socket
        # closing and uvicorn re-binding, another process can claim the port.
        # Rare in practice, but pytest's xdist parallel runners do hit it.
        # Retry the bind a few times with a fresh port pick before giving up.
        #
        # Each retry adds a small random sleep on top of the constant 2-second
        # readiness budget so two A2A servers losing the same race don't keep
        # losing it: without jitter, both fall into lockstep and both retry
        # at the same instant against the same kernel allocator.
        import random
        last_exc: Exception | None = None
        for attempt in range(5):
            config = uvicorn.Config(
                self._app, host=self._host, port=self._port,
                log_level="warning", access_log=False,
            )
            self._server = uvicorn.Server(config)
            self._task = asyncio.create_task(self._server.serve())
            # Wait until uvicorn is ready to accept connections (or fail fast
            # if the bind threw — `.started` stays False forever in that case
            # but the task transitions to done with the exception).
            for _ in range(200):  # ~2 seconds max
                if self._server.started:
                    return
                if self._task.done():
                    break
                await asyncio.sleep(0.01)
            if self._server.started:
                return
            # Task finished without `started=True` → bind failed (typically
            # OSError EADDRINUSE). Capture, repick port, retry with jitter.
            if self._task.done():
                try:
                    self._task.result()
                except Exception as exc:
                    last_exc = exc
            self._port = _pick_free_port()
            await asyncio.sleep(random.uniform(0.05, 0.25))
        raise RuntimeError(
            f"A2A server failed to bind after 5 attempts (last error: {last_exc!r})"
        )

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                # Consume the CancelledError so asyncio doesn't log
                # "Task was destroyed but it is pending" on event-loop shutdown.
                # An unrelated exception escaping a cancelled uvicorn task
                # would also normally surface here; we let it propagate so it
                # isn't silently lost, but CancelledError is expected.
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
