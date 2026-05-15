from __future__ import annotations
import asyncio
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable
import uvicorn
from fastapi import FastAPI, Request


HandlerFunc = Callable[[str, dict, dict], Awaitable[dict]]


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


def _pick_free_port() -> int:
    """Bind a socket to port 0, get the assigned port, then close the socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class A2AServer:
    def __init__(self, *, handler: A2AHandler, host: str = "127.0.0.1", port: int = 0):
        self._handler = handler
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

    async def start(self) -> None:
        config = uvicorn.Config(
            self._app, host=self._host, port=self._port,
            log_level="warning", access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        # Wait until uvicorn is ready to accept connections.
        for _ in range(200):  # ~2 seconds max
            if self._server.started:
                break
            await asyncio.sleep(0.01)

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
