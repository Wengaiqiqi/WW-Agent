"""Async JSON-RPC-over-stdio client that drives a `hermes acp` subprocess.

Mirrors the wire protocol in hermes-agent/agent/copilot_acp_client.py (camelCase
ACP). Hand-rolled JSON-RPC — no dependency on the `acp` python package on the
bridge side. The `hermes acp` server still needs Hermes' own `[acp]` extra.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)

ACP_PROTOCOL_VERSION = 1


class ACPError(Exception):
    pass


def _translate_update(update: dict) -> dict | None:
    """Translate one ACP `session/update` payload into an A2A SSE event.

    Only agent_message_chunk / agent_thought_chunk are relied upon (stable
    across Hermes versions). Tool events are best-effort enrichment.
    """
    kind = str(update.get("sessionUpdate") or "")
    content = update.get("content") or {}
    text = content.get("text") if isinstance(content, dict) else None
    if kind == "agent_message_chunk" and text:
        return {"type": "text", "text": text}
    if kind == "agent_thought_chunk" and text:
        return {"type": "thinking", "text": text}
    if kind == "tool_call":
        return {"type": "tool_call",
                "id": update.get("toolCallId"),
                "name": update.get("title") or update.get("toolName") or "tool"}
    if kind == "tool_call_update":
        return {"type": "tool_result",
                "id": update.get("toolCallId"),
                "status": update.get("status")}
    return None


class HermesACPClient:
    def __init__(
        self,
        *,
        argv: list[str] | None = None,
        command: str | None = None,
        workdir: str | None = None,
        auto_approve: bool = False,
    ):
        if argv is not None:
            self._argv = list(argv)
        else:
            cmd = command or os.environ.get("HERMES_ACP_CMD", "hermes acp")
            self._argv = shlex.split(cmd)
        self._workdir = workdir or os.environ.get("HERMES_A2A_WORKDIR") or os.getcwd()
        self._auto_approve = auto_approve

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}

        self._known_sessions: set[str] = set()
        self._session_queues: dict[str, asyncio.Queue] = {}
        self._session_text: dict[str, str] = {}
        self._session_prompt: dict[str, str] = {}
        self._running: dict[str, bool] = {}

    # ---- process lifecycle --------------------------------------------------

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._workdir,
            )
            self._pending.clear()
            self._known_sessions.clear()
            self._reader_task = asyncio.create_task(self._read_loop())
            await self._initialize()

    async def aclose(self) -> None:
        proc = self._proc
        self._proc = None
        if self._reader_task is not None:
            self._reader_task.cancel()
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    # ---- JSON-RPC plumbing --------------------------------------------------

    def _send(self, obj: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))

    async def _request(self, method: str, params: dict) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        assert self._proc is not None and self._proc.stdin is not None
        await self._proc.stdin.drain()
        return await fut

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break  # EOF — process exited
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(msg)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ACPError("hermes acp process exited"))
            self._pending.clear()

    async def _dispatch(self, msg: dict) -> None:
        if "method" in msg:
            await self._handle_incoming(msg)
            return
        fut = self._pending.pop(msg.get("id"), None)
        if fut is not None and not fut.done():
            if "error" in msg:
                fut.set_exception(ACPError(str(msg["error"])))
            else:
                fut.set_result(msg.get("result"))

    async def _handle_incoming(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/update":
            sid = params.get("sessionId") or ""
            ev = _translate_update(params.get("update") or {})
            if ev is not None:
                if ev.get("type") == "text":
                    self._session_text[sid] = self._session_text.get(sid, "") + ev["text"]
                q = self._session_queues.get(sid)
                if q is not None:
                    await q.put(ev)
            return
        # Server->client request — must answer (it carries an id).
        mid = msg.get("id")
        if method == "session/request_permission":
            outcome: dict
            if self._auto_approve:
                options = params.get("options") or []
                allow = next((o for o in options
                              if "allow" in str(o.get("optionId", "")).lower()), None)
                opt_id = (allow or (options[0] if options else {})).get("optionId", "allow")
                outcome = {"outcome": "selected", "optionId": opt_id}
            else:
                outcome = {"outcome": "cancelled"}
            self._send({"jsonrpc": "2.0", "id": mid, "result": {"outcome": outcome}})
        else:
            # We advertise no fs capabilities; refuse anything else politely.
            self._send({"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601,
                                  "message": f"bridge does not support {method}"}})
        assert self._proc is not None and self._proc.stdin is not None
        await self._proc.stdin.drain()

    async def _initialize(self) -> None:
        result = await self._request("initialize", {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            "clientInfo": {"name": "hermes-a2a-bridge", "version": "1.0.0"},
        }) or {}
        auth_methods = result.get("authMethods") or []
        if auth_methods:
            method_id = auth_methods[0].get("id")
            if method_id:
                try:
                    await self._request("authenticate", {"methodId": method_id})
                except ACPError:
                    log.warning("ACP authenticate failed; continuing unauthenticated")

    # ---- sessions -----------------------------------------------------------

    async def ensure_session(self, context_id: str | None) -> str:
        await self._ensure_started()
        if context_id and context_id in self._known_sessions:
            return context_id
        result = await self._request("session/new",
                                     {"cwd": self._workdir, "mcpServers": []}) or {}
        sid = str(result.get("sessionId") or "")
        if not sid:
            raise ACPError("hermes acp did not return a sessionId")
        self._known_sessions.add(sid)
        return sid
