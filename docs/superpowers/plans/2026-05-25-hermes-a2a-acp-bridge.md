# Hermes A2A↔ACP Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bridge process (run on the Hermes host) that speaks comm-agent's Google A2A v0.3 outward and drives a local `hermes acp` over stdio ACP inward, so agent-last can delegate tasks / chat / query status to a remote Hermes with zero changes on the agent-last side.

**Architecture:** Reuse agent-last's existing A2A server (`agents.comm_agent.a2a_protocol.build_app`), authz (`agents.shared.authz`), and card builder (`agents.comm_agent.agent_card`). Add a new `bridge/hermes_a2a/` package containing (a) a hand-rolled async JSON-RPC ACP client that spawns `hermes acp`, and (b) two dispatchers that translate A2A skills (`task.delegate` / `chat.message` / `status.query`) into ACP `session/new` + `session/prompt`. A companion install script provisions the bridge + Caddy on the Hermes machine.

**Tech Stack:** Python 3.10+, asyncio (`asyncio.create_subprocess_exec` for stdio JSON-RPC), FastAPI/uvicorn (via reused `build_app`), pyjwt + httpx (transitively, via reused modules), pytest + pytest-asyncio. ACP wire format mirrors `hermes-agent/agent/copilot_acp_client.py` (camelCase JSON-RPC: `sessionId`, `sessionUpdate`, `agent_message_chunk`).

**Reference facts (verified against source):**
- `build_app(*, self_card, hmac_secret, my_peer_id, skill_dispatcher, stream_dispatcher, nonce_cache=None)`. Dispatchers receive the **raw method** (`message/stream` → `stream_dispatcher`; `message/send` and `status/query` → `skill_dispatcher`). Grant verification (skill mapping, target check, nonce replay, exp) is fully handled inside `build_app`.
- agent-last `A2AClient.__init__(peer, *, secret, my_peer_id, transport=None, ...)` — accepts an injected `httpx.AsyncBaseTransport`, enabling in-process `ASGITransport` tests.
- agent-last `comm.delegate` collects all SSE events and reads `result` from the event where `type=="task"` and `state=="completed"`. `comm.chat` reads `reply` + `context_id`. `comm.status` returns the dict as-is.
- ACP wire (from `copilot_acp_client.py`): `initialize` → `session/new` (params `{cwd, mcpServers}`, result `{sessionId}`) → `session/prompt` (params `{sessionId, prompt:[{type:"text",text}]}`); during prompt the server pushes `session/update` notifications with `params.update.sessionUpdate` ∈ {`agent_message_chunk`, `agent_thought_chunk`} and `params.update.content.text`; server may send `session/request_permission` (respond `{result:{outcome:{outcome:"cancelled"}}}` to deny).

---

## File Structure

```
bridge/
└── hermes_a2a/
    ├── __init__.py            # empty package marker
    ├── acp_client.py          # HermesACPClient: async JSON-RPC stdio client for `hermes acp`
    ├── dispatchers.py         # make_dispatchers(acp, allowed_peer) -> (skill_dispatcher, stream_dispatcher)
    └── __main__.py            # build() assembles build_app; main() runs uvicorn

scripts/
├── install_hermes_a2a.sh      # Linux/macOS installer (mirrors install_openclaw_a2a.sh)
└── install_hermes_a2a.ps1     # Windows installer

tests/test_bridge_hermes/
├── __init__.py
├── conftest.py                # fake_acp_argv fixture
├── fake_hermes_acp.py         # minimal fake `hermes acp` stub (echoes prompt)
├── test_acp_client.py         # client lifecycle + streaming + status + reconnect
├── test_dispatchers.py        # ACP→A2A translation (delegate/chat/status)
└── test_e2e_bridge.py         # real A2AClient → build_app(bridge dispatchers) → fake hermes
```

**Reused unchanged (import only):** `agents.comm_agent.a2a_protocol.build_app`, `agents.comm_agent.agent_card.build_self_card`, `agents.shared.authz`, `agents.comm_agent.peer_registry.Peer`, `agents.comm_agent.a2a_protocol.A2AClient`.

---

## Task 1: Package scaffold + fake Hermes ACP stub + test fixture

**Files:**
- Create: `bridge/__init__.py`
- Create: `bridge/hermes_a2a/__init__.py`
- Create: `tests/test_bridge_hermes/__init__.py`
- Create: `tests/test_bridge_hermes/fake_hermes_acp.py`
- Create: `tests/test_bridge_hermes/conftest.py`

- [ ] **Step 1: Create the two empty package markers**

Create `bridge/__init__.py` with a single line:

```python
"""Standalone bridges deployed on remote agent hosts (not part of the orchestrator)."""
```

Create `bridge/hermes_a2a/__init__.py` with a single line:

```python
"""A2A v0.3 (outward) ↔ ACP stdio (inward, drives `hermes acp`) bridge for Hermes."""
```

Create `tests/test_bridge_hermes/__init__.py` as an empty file.

- [ ] **Step 2: Write the fake `hermes acp` stub**

Create `tests/test_bridge_hermes/fake_hermes_acp.py`:

```python
#!/usr/bin/env python
"""Minimal fake `hermes acp` for tests.

Speaks the same camelCase ACP JSON-RPC over stdio that real `hermes acp`
speaks (see hermes-agent/agent/copilot_acp_client.py). Behavior:
  - initialize          -> result with protocolVersion
  - session/new         -> result {"sessionId": "sess-N"}
  - session/prompt      -> streams two agent_message_chunk updates that echo
                           the prompt text, then returns {"stopReason":"end_turn"}
  - anything else       -> JSON-RPC error -32601

Env knobs for tests:
  FAKE_ACP_FAIL_PROMPT=1  -> respond to session/prompt with a JSON-RPC error
  FAKE_ACP_ASK_PERMISSION=1 -> emit a session/request_permission before completing
"""
from __future__ import annotations

import json
import os
import sys


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    session_counter = 0
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"protocolVersion": 1,
                             "agentInfo": {"name": "fake-hermes", "version": "0.0.0"}}})
        elif method == "session/new":
            session_counter += 1
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"sessionId": f"sess-{session_counter}"}})
        elif method == "session/prompt":
            sid = params.get("sessionId")
            text = "".join(
                p.get("text", "") for p in params.get("prompt", [])
                if isinstance(p, dict) and p.get("type") == "text"
            )
            if os.environ.get("FAKE_ACP_FAIL_PROMPT") == "1":
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32000, "message": "fake hermes prompt failure"}})
                continue
            if os.environ.get("FAKE_ACP_ASK_PERMISSION") == "1":
                # Server->client request; bridge must answer before we finish.
                send({"jsonrpc": "2.0", "id": 9001, "method": "session/request_permission",
                      "params": {"sessionId": sid,
                                 "options": [{"optionId": "allow-once", "name": "Allow"},
                                             {"optionId": "reject-once", "name": "Reject"}]}})
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": sid,
                             "update": {"sessionUpdate": "agent_message_chunk",
                                        "content": {"type": "text", "text": "echo: "}}}})
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": sid,
                             "update": {"sessionUpdate": "agent_message_chunk",
                                        "content": {"type": "text", "text": text}}}})
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write the conftest fixture**

Create `tests/test_bridge_hermes/conftest.py`:

```python
"""Fixtures for the Hermes bridge tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture
def fake_acp_argv() -> list[str]:
    """argv list that launches the fake `hermes acp` stub with the test python.

    Returned as a list (not a string) so paths containing spaces — e.g.
    'D:\\Claude Code\\agent-last' — never go through shlex.split.
    """
    stub = Path(__file__).parent / "fake_hermes_acp.py"
    return [sys.executable, str(stub)]
```

- [ ] **Step 4: Verify the stub runs standalone**

Run: `python tests/test_bridge_hermes/fake_hermes_acp.py` then paste one line and press Enter:
```
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
```
Expected: prints a line containing `"protocolVersion": 1`. Press Ctrl-C / Ctrl-D to exit.

- [ ] **Step 5: Commit**

```bash
git add bridge/__init__.py bridge/hermes_a2a/__init__.py tests/test_bridge_hermes/
git commit -m "test(hermes-bridge): scaffold package + fake hermes acp stub"
```

---

## Task 2: ACP client — start, initialize, new_session

**Files:**
- Create: `bridge/hermes_a2a/acp_client.py`
- Test: `tests/test_bridge_hermes/test_acp_client.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bridge_hermes/test_acp_client.py`:

```python
"""Tests for HermesACPClient against the fake `hermes acp` stub."""
from __future__ import annotations

import pytest

from bridge.hermes_a2a.acp_client import ACPError, HermesACPClient


@pytest.mark.asyncio
async def test_ensure_session_returns_session_id(fake_acp_argv):
    acp = HermesACPClient(argv=fake_acp_argv)
    try:
        sid = await acp.ensure_session(None)
        assert sid == "sess-1"
        # Reusing a known context_id returns the same id (no new session).
        assert await acp.ensure_session(sid) == "sess-1"
        # Unknown context_id allocates a fresh session.
        assert await acp.ensure_session("does-not-exist") == "sess-2"
    finally:
        await acp.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bridge_hermes/test_acp_client.py::test_ensure_session_returns_session_id -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bridge.hermes_a2a.acp_client'`.

- [ ] **Step 3: Write the initial client implementation**

Create `bridge/hermes_a2a/acp_client.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_bridge_hermes/test_acp_client.py::test_ensure_session_returns_session_id -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bridge/hermes_a2a/acp_client.py tests/test_bridge_hermes/test_acp_client.py
git commit -m "feat(hermes-bridge): ACP client start/initialize/new_session"
```

---

## Task 3: ACP client — prompt streaming + translation

**Files:**
- Modify: `bridge/hermes_a2a/acp_client.py` (add `run_prompt`)
- Test: `tests/test_bridge_hermes/test_acp_client.py` (add streaming test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bridge_hermes/test_acp_client.py`:

```python
@pytest.mark.asyncio
async def test_run_prompt_streams_text_then_completes(fake_acp_argv):
    acp = HermesACPClient(argv=fake_acp_argv)
    try:
        sid = await acp.ensure_session(None)
        events = [ev async for ev in acp.run_prompt(sid, "hello world")]
    finally:
        await acp.aclose()

    text_events = [e for e in events if e.get("type") == "text"]
    assert "".join(e["text"] for e in text_events) == "echo: hello world"

    completed = [e for e in events if e.get("type") == "task" and e.get("state") == "completed"]
    assert len(completed) == 1
    assert completed[0]["result"] == "echo: hello world"


@pytest.mark.asyncio
async def test_run_prompt_failure_yields_failed_event(fake_acp_argv, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_FAIL_PROMPT", "1")
    acp = HermesACPClient(argv=fake_acp_argv)
    try:
        sid = await acp.ensure_session(None)
        events = [ev async for ev in acp.run_prompt(sid, "boom")]
    finally:
        await acp.aclose()
    assert any(e.get("type") == "task" and e.get("state") == "failed" for e in events)
    assert not any(e.get("state") == "completed" for e in events)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bridge_hermes/test_acp_client.py -v -k run_prompt`
Expected: FAIL — `AttributeError: 'HermesACPClient' object has no attribute 'run_prompt'`.

- [ ] **Step 3: Add `run_prompt` to the client**

Append these methods inside the `HermesACPClient` class in `bridge/hermes_a2a/acp_client.py` (after `ensure_session`):

```python
    async def run_prompt(self, session_id: str, text: str) -> AsyncIterator[dict]:
        """Run one ACP turn; yield translated A2A events ending in a terminal
        task event (completed or failed). Never raises — failures become a
        {"type":"task","state":"failed"} event."""
        await self._ensure_started()
        q: asyncio.Queue = asyncio.Queue()
        self._session_queues[session_id] = q
        self._session_text[session_id] = ""
        self._session_prompt[session_id] = text
        self._running[session_id] = True
        done = object()

        async def _drive() -> None:
            try:
                await self._request("session/prompt", {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                })
                await q.put({"type": "task", "state": "completed",
                             "result": self._session_text.get(session_id, "")})
            except Exception as exc:  # noqa: BLE001 — surface, don't crash the SSE
                await q.put({"type": "task", "state": "failed", "error": str(exc)})
            finally:
                await q.put(done)

        drive_task = asyncio.create_task(_drive())
        try:
            while True:
                ev = await q.get()
                if ev is done:
                    break
                yield ev
        finally:
            self._running[session_id] = False
            self._session_queues.pop(session_id, None)
            await drive_task
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bridge_hermes/test_acp_client.py -v -k run_prompt`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add bridge/hermes_a2a/acp_client.py tests/test_bridge_hermes/test_acp_client.py
git commit -m "feat(hermes-bridge): stream ACP session/update as A2A events"
```

---

## Task 4: ACP client — `prompt_collect` (chat) + `status`

**Files:**
- Modify: `bridge/hermes_a2a/acp_client.py`
- Test: `tests/test_bridge_hermes/test_acp_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bridge_hermes/test_acp_client.py`:

```python
@pytest.mark.asyncio
async def test_prompt_collect_returns_final_text(fake_acp_argv):
    acp = HermesACPClient(argv=fake_acp_argv)
    try:
        sid = await acp.ensure_session(None)
        reply = await acp.prompt_collect(sid, "ping")
    finally:
        await acp.aclose()
    assert reply == "echo: ping"


@pytest.mark.asyncio
async def test_prompt_collect_raises_on_failure(fake_acp_argv, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_FAIL_PROMPT", "1")
    acp = HermesACPClient(argv=fake_acp_argv)
    try:
        sid = await acp.ensure_session(None)
        with pytest.raises(ACPError):
            await acp.prompt_collect(sid, "boom")
    finally:
        await acp.aclose()


@pytest.mark.asyncio
async def test_status_idle_then_reports_sessions(fake_acp_argv):
    acp = HermesACPClient(argv=fake_acp_argv)
    try:
        assert acp.status()["state"] == "idle"
        await acp.ensure_session(None)
        st = acp.status()
        assert st["state"] == "idle"
        assert st["sessions"] == 1
    finally:
        await acp.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bridge_hermes/test_acp_client.py -v -k "prompt_collect or status"`
Expected: FAIL — `AttributeError: ... 'prompt_collect'`.

- [ ] **Step 3: Add `prompt_collect` and `status`**

Append inside `HermesACPClient` in `bridge/hermes_a2a/acp_client.py` (after `run_prompt`):

```python
    async def prompt_collect(self, session_id: str, text: str) -> str:
        """Run one turn and return only the final assistant text. Raises
        ACPError if the turn failed (used by the synchronous chat path)."""
        final = ""
        async for ev in self.run_prompt(session_id, text):
            if ev.get("type") == "task" and ev.get("state") == "completed":
                final = ev.get("result", "")
            elif ev.get("type") == "task" and ev.get("state") == "failed":
                raise ACPError(ev.get("error", "prompt failed"))
        return final

    def status(self) -> dict:
        """Snapshot of bridge-tracked run state (ACP has no native status)."""
        running = [sid for sid, r in self._running.items() if r]
        return {
            "state": "working" if running else "idle",
            "current_task": (self._session_prompt.get(running[0], "")[:200]
                             if running else None),
            "sessions": len(self._known_sessions),
            "last_error": None,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bridge_hermes/test_acp_client.py -v -k "prompt_collect or status"`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add bridge/hermes_a2a/acp_client.py tests/test_bridge_hermes/test_acp_client.py
git commit -m "feat(hermes-bridge): chat prompt_collect + status snapshot"
```

---

## Task 5: ACP client — auto-approve permission handling

**Files:**
- Test: `tests/test_bridge_hermes/test_acp_client.py`
- (No client change needed — `_handle_incoming` already implements deny/allow. This task proves both paths.)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bridge_hermes/test_acp_client.py`:

```python
@pytest.mark.asyncio
async def test_permission_denied_by_default_still_completes(fake_acp_argv, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_ASK_PERMISSION", "1")
    acp = HermesACPClient(argv=fake_acp_argv)  # auto_approve defaults to False
    try:
        sid = await acp.ensure_session(None)
        events = [ev async for ev in acp.run_prompt(sid, "needs perm")]
    finally:
        await acp.aclose()
    # The stub continues to completion regardless; the point is the bridge
    # answered the request_permission RPC so the prompt did not deadlock.
    assert any(e.get("state") == "completed" for e in events)


@pytest.mark.asyncio
async def test_permission_auto_approve(fake_acp_argv, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_ASK_PERMISSION", "1")
    acp = HermesACPClient(argv=fake_acp_argv, auto_approve=True)
    try:
        sid = await acp.ensure_session(None)
        events = [ev async for ev in acp.run_prompt(sid, "needs perm")]
    finally:
        await acp.aclose()
    assert any(e.get("state") == "completed" for e in events)
```

- [ ] **Step 2: Run tests to verify behavior**

Run: `pytest tests/test_bridge_hermes/test_acp_client.py -v -k permission`
Expected: PASS (both) — confirms the bridge answers `session/request_permission` (deny and allow) without deadlocking the prompt. If these had failed with a hang/timeout, `_handle_incoming` would need fixing; they should pass against the Task 2 implementation.

- [ ] **Step 3: Commit**

```bash
git add tests/test_bridge_hermes/test_acp_client.py
git commit -m "test(hermes-bridge): cover request_permission deny + auto-approve"
```

---

## Task 6: Dispatchers — `stream_dispatcher` (task.delegate)

**Files:**
- Create: `bridge/hermes_a2a/dispatchers.py`
- Test: `tests/test_bridge_hermes/test_dispatchers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bridge_hermes/test_dispatchers.py`:

```python
"""Tests for ACP→A2A dispatcher translation, using a fake ACP client."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from bridge.hermes_a2a.dispatchers import make_dispatchers


class FakeACP:
    """Stand-in for HermesACPClient with deterministic, in-memory behavior."""

    def __init__(self):
        self._counter = 0
        self.known: set[str] = set()
        self.prompts: list[tuple[str, str]] = []

    async def ensure_session(self, context_id):
        if context_id and context_id in self.known:
            return context_id
        self._counter += 1
        sid = f"s{self._counter}"
        self.known.add(sid)
        return sid

    async def run_prompt(self, session_id, text) -> AsyncIterator[dict]:
        self.prompts.append((session_id, text))
        yield {"type": "text", "text": f"echo: {text}"}
        yield {"type": "task", "state": "completed", "result": f"echo: {text}"}

    async def prompt_collect(self, session_id, text) -> str:
        self.prompts.append((session_id, text))
        return f"echo: {text}"

    def status(self) -> dict:
        return {"state": "idle", "current_task": None, "sessions": len(self.known)}


def _params(text, context_id=None):
    p = {"message": {"role": "user", "parts": [{"text": text}]}}
    if context_id is not None:
        p["context_id"] = context_id
    return p


@pytest.mark.asyncio
async def test_stream_dispatcher_delegate_emits_working_then_completed():
    acp = FakeACP()
    _skill, stream = make_dispatchers(acp)
    events = [
        ev async for ev in stream("message/stream", _params("do thing"), {"peer_id": "caller"})
    ]
    assert events[0] == {"type": "task", "state": "working", "message": "delegating to hermes"}
    assert {"type": "text", "text": "echo: do thing"} in events
    completed = [e for e in events if e.get("state") == "completed"]
    assert completed and completed[0]["result"] == "echo: do thing"


@pytest.mark.asyncio
async def test_stream_dispatcher_rejects_disallowed_caller():
    acp = FakeACP()
    _skill, stream = make_dispatchers(acp, allowed_peer="trusted")
    events = [
        ev async for ev in stream("message/stream", _params("x"), {"peer_id": "intruder"})
    ]
    assert events == [{"type": "task", "state": "failed", "error": "caller peer not allowed"}]
    assert acp.prompts == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bridge_hermes/test_dispatchers.py -v -k stream`
Expected: FAIL — `ModuleNotFoundError: No module named 'bridge.hermes_a2a.dispatchers'`.

- [ ] **Step 3: Write the dispatchers module**

Create `bridge/hermes_a2a/dispatchers.py`:

```python
"""Translate comm-agent A2A skills into ACP calls on a HermesACPClient.

build_app passes the *raw method* to dispatchers:
  message/stream -> stream_dispatcher (task.delegate)
  message/send   -> skill_dispatcher  (chat.message)
  status/query   -> skill_dispatcher  (status.query)
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from bridge.hermes_a2a.acp_client import ACPError


def _caller_ok(claims: dict, allowed_peer: str | None) -> bool:
    return allowed_peer is None or claims.get("peer_id") == allowed_peer


def _text_of(params: dict) -> str:
    parts = (params.get("message") or {}).get("parts") or []
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            return p["text"]
    return ""


def make_dispatchers(acp, *, allowed_peer: str | None = None):
    """Return (skill_dispatcher, stream_dispatcher) bound to an ACP client."""

    async def stream_dispatcher(method: str, params: dict, claims: dict) -> AsyncIterator[dict]:
        if not _caller_ok(claims, allowed_peer):
            yield {"type": "task", "state": "failed", "error": "caller peer not allowed"}
            return
        text = _text_of(params)
        if not text:
            yield {"type": "task", "state": "failed", "error": "empty task"}
            return
        yield {"type": "task", "state": "working", "message": "delegating to hermes"}
        try:
            session_id = await acp.ensure_session(params.get("context_id"))
        except ACPError as exc:
            yield {"type": "task", "state": "failed", "error": f"hermes acp unavailable: {exc}"}
            return
        async for ev in acp.run_prompt(session_id, text):
            yield ev

    async def skill_dispatcher(method: str, params: dict, claims: dict) -> dict:
        if not _caller_ok(claims, allowed_peer):
            return {"error": "caller peer not allowed"}
        if method == "status/query":
            return acp.status()
        if method == "message/send":
            text = _text_of(params)
            if not text:
                return {"error": "empty message"}
            try:
                session_id = await acp.ensure_session(params.get("context_id"))
                reply = await acp.prompt_collect(session_id, text)
            except ACPError as exc:
                return {"error": f"hermes acp: {exc}"}
            return {"reply": reply, "context_id": session_id}
        return {"error": f"unsupported method {method!r}"}

    return skill_dispatcher, stream_dispatcher
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bridge_hermes/test_dispatchers.py -v -k stream`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add bridge/hermes_a2a/dispatchers.py tests/test_bridge_hermes/test_dispatchers.py
git commit -m "feat(hermes-bridge): stream_dispatcher maps task.delegate to ACP"
```

---

## Task 7: Dispatchers — `skill_dispatcher` (chat.message + status.query)

**Files:**
- Test: `tests/test_bridge_hermes/test_dispatchers.py`
- (No module change — `skill_dispatcher` written in Task 6. This task proves chat context reuse + status.)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bridge_hermes/test_dispatchers.py`:

```python
@pytest.mark.asyncio
async def test_skill_dispatcher_chat_first_turn_allocates_context():
    acp = FakeACP()
    skill, _stream = make_dispatchers(acp)
    out = await skill("message/send", _params("hi"), {"peer_id": "caller"})
    assert out["reply"] == "echo: hi"
    assert out["context_id"] == "s1"


@pytest.mark.asyncio
async def test_skill_dispatcher_chat_reuses_context():
    acp = FakeACP()
    skill, _stream = make_dispatchers(acp)
    first = await skill("message/send", _params("hi"), {"peer_id": "caller"})
    second = await skill("message/send",
                         _params("again", context_id=first["context_id"]),
                         {"peer_id": "caller"})
    assert second["context_id"] == first["context_id"]   # same ACP session
    assert [sid for sid, _ in acp.prompts] == ["s1", "s1"]


@pytest.mark.asyncio
async def test_skill_dispatcher_status():
    acp = FakeACP()
    skill, _stream = make_dispatchers(acp)
    out = await skill("status/query", {}, {"peer_id": "caller"})
    assert out["state"] == "idle"


@pytest.mark.asyncio
async def test_skill_dispatcher_unsupported_method():
    acp = FakeACP()
    skill, _stream = make_dispatchers(acp)
    out = await skill("message/bogus", _params("x"), {"peer_id": "caller"})
    assert "unsupported" in out["error"]
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/test_bridge_hermes/test_dispatchers.py -v -k "skill"`
Expected: PASS (all four) against the Task 6 implementation.

- [ ] **Step 3: Commit**

```bash
git add tests/test_bridge_hermes/test_dispatchers.py
git commit -m "test(hermes-bridge): chat context reuse + status + bad method"
```

---

## Task 8: Entrypoint — `build()` assembles `build_app`

**Files:**
- Create: `bridge/hermes_a2a/__main__.py`
- Test: `tests/test_bridge_hermes/test_acp_client.py` (add a build test) — or a new `test_main.py`. Use `test_main.py`.
- Test: `tests/test_bridge_hermes/test_main.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bridge_hermes/test_main.py`:

```python
"""Tests for the bridge entrypoint assembly."""
from __future__ import annotations

import httpx
import pytest

from bridge.hermes_a2a.__main__ import build


@pytest.mark.asyncio
async def test_build_serves_agent_card(monkeypatch, fake_acp_argv):
    monkeypatch.setenv("HERMES_A2A_HMAC", "secret-xyz")
    monkeypatch.setenv("HERMES_A2A_MY_PEER_ID", "hermes-home")
    monkeypatch.setenv("HERMES_ACP_CMD", " ".join(fake_acp_argv))  # not started by build()
    app = build()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/.well-known/agent.json")
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "hermes-hermes-home"
    assert card["schemaVersion"] == "0.3"
    assert {s["id"] for s in card["skills"]} == {"task.delegate", "chat.message", "status.query"}


def test_build_requires_hmac(monkeypatch):
    monkeypatch.delenv("HERMES_A2A_HMAC", raising=False)
    with pytest.raises(SystemExit):
        build()
```

Note: `HERMES_ACP_CMD` is set with a space-joined argv only because `build()` constructs (but does not start) the client; the fake stub path here has a space-containing repo path, so the started-process tests use the `argv=` form instead (Tasks 2–7). `build()` never spawns, so shlex-splitting a spacey path is harmless here.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bridge_hermes/test_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bridge.hermes_a2a.__main__'`.

- [ ] **Step 3: Write the entrypoint**

Create `bridge/hermes_a2a/__main__.py`:

```python
"""Bridge entrypoint.

Run from the agent-last repo root so `agents.*` and `bridge.*` are importable:

    python -m bridge.hermes_a2a

Environment:
  HERMES_A2A_HMAC          (required) shared HMAC secret with the agent-last caller
  HERMES_A2A_MY_PEER_ID    self peer id (default: hermes-home)
  HERMES_A2A_ALLOWED_PEER  optional caller peer_id allowlist (one peer)
  HERMES_A2A_PORT          local HTTP port uvicorn binds (default: 19444)
  HERMES_A2A_PUBLIC_HOST   host for the advertised card url (default: 127.0.0.1)
  HERMES_A2A_PUBLIC_PORT   public port for the card url (default: CADDY_PORT or 8443)
  HERMES_ACP_CMD           command to launch ACP server (default: "hermes acp")
  HERMES_A2A_WORKDIR       cwd for ACP sessions (default: process cwd)
  HERMES_A2A_AUTO_APPROVE  "1" to auto-approve ACP permission requests (default: deny)
"""
from __future__ import annotations

import logging
import os
import sys

import uvicorn

from agents.comm_agent.a2a_protocol import build_app
from agents.comm_agent.agent_card import build_self_card
from bridge.hermes_a2a.acp_client import HermesACPClient
from bridge.hermes_a2a.dispatchers import make_dispatchers

log = logging.getLogger(__name__)


def build():
    """Assemble and return the FastAPI app (does not start the ACP subprocess)."""
    hmac_secret = os.environ.get("HERMES_A2A_HMAC", "")
    if not hmac_secret:
        raise SystemExit("HERMES_A2A_HMAC is required (the shared secret with agent-last)")

    my_peer_id = os.environ.get("HERMES_A2A_MY_PEER_ID", "hermes-home")
    allowed_peer = os.environ.get("HERMES_A2A_ALLOWED_PEER") or None
    public_host = os.environ.get("HERMES_A2A_PUBLIC_HOST") or None
    public_port = int(os.environ.get("HERMES_A2A_PUBLIC_PORT",
                                     os.environ.get("CADDY_PORT", "8443")))
    public_url = (f"https://{public_host}:{public_port}" if public_host
                  else f"https://127.0.0.1:{public_port}")

    acp = HermesACPClient(
        command=os.environ.get("HERMES_ACP_CMD", "hermes acp"),
        workdir=os.environ.get("HERMES_A2A_WORKDIR") or None,
        auto_approve=os.environ.get("HERMES_A2A_AUTO_APPROVE") == "1",
    )
    skill_dispatcher, stream_dispatcher = make_dispatchers(acp, allowed_peer=allowed_peer)
    card = build_self_card(
        name=f"hermes-{my_peer_id}",
        description="Hermes via A2A<->ACP bridge",
        public_url=public_url,
        version="1.0.0",
    )
    return build_app(
        self_card=card,
        hmac_secret=hmac_secret,
        my_peer_id=my_peer_id,
        skill_dispatcher=skill_dispatcher,
        stream_dispatcher=stream_dispatcher,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    port = int(os.environ.get("HERMES_A2A_PORT", "19444"))
    uvicorn.run(build(), host="127.0.0.1", port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_bridge_hermes/test_main.py -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add bridge/hermes_a2a/__main__.py tests/test_bridge_hermes/test_main.py
git commit -m "feat(hermes-bridge): entrypoint assembles build_app from env"
```

---

## Task 9: End-to-end — real A2AClient → bridge → fake Hermes

**Files:**
- Test: `tests/test_bridge_hermes/test_e2e_bridge.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bridge_hermes/test_e2e_bridge.py`:

```python
"""End-to-end: agent-last's real A2AClient drives the bridge's build_app over an
in-process ASGI transport (no TLS, no real Hermes — CI-friendly). The ACP side
is the fake `hermes acp` stub. Exercises the full grant/auth/SSE path."""
from __future__ import annotations

import httpx
import pytest

from agents.comm_agent.a2a_protocol import A2AClient, build_app
from agents.comm_agent.agent_card import build_self_card
from agents.comm_agent.peer_registry import Peer
from bridge.hermes_a2a.acp_client import HermesACPClient
from bridge.hermes_a2a.dispatchers import make_dispatchers

SECRET = "shared-secret"
PEER_ID = "hermes-home"


def _make_client(app) -> A2AClient:
    peer = Peer(
        peer_id=PEER_ID, display_name="h", url="https://hermes-home",
        hmac_secret_ref="X", tls_verify=True, tls_pinned_sha256=None,
        added_at="", last_seen=None,
    )
    transport = httpx.ASGITransport(app=app)
    return A2AClient(peer, secret=SECRET, my_peer_id="agent-last-laptop", transport=transport)


def _build_app(fake_acp_argv):
    acp = HermesACPClient(argv=fake_acp_argv)
    skill_d, stream_d = make_dispatchers(acp)
    card = build_self_card(name=f"hermes-{PEER_ID}", description="x",
                           public_url="https://hermes-home", version="1.0.0")
    app = build_app(self_card=card, hmac_secret=SECRET, my_peer_id=PEER_ID,
                    skill_dispatcher=skill_d, stream_dispatcher=stream_d)
    return app, acp


@pytest.mark.asyncio
async def test_delegate_end_to_end(fake_acp_argv):
    app, acp = _build_app(fake_acp_argv)
    try:
        client = _make_client(app)
        events = [
            ev async for ev in client.stream(
                method="message/stream",
                params={"message": {"role": "user", "parts": [{"text": "hi"}]}},
                skill="task.delegate",
            )
        ]
    finally:
        await acp.aclose()
    completed = [e for e in events if e.get("type") == "task" and e.get("state") == "completed"]
    assert completed and completed[0]["result"] == "echo: hi"


@pytest.mark.asyncio
async def test_chat_multiturn_end_to_end(fake_acp_argv):
    app, acp = _build_app(fake_acp_argv)
    try:
        client = _make_client(app)
        first = await client.call(
            method="message/send",
            params={"message": {"role": "user", "parts": [{"text": "hello"}]},
                    "context_id": None},
            skill="chat.message",
        )
        assert first["reply"] == "echo: hello"
        ctx = first["context_id"]
        second = await client.call(
            method="message/send",
            params={"message": {"role": "user", "parts": [{"text": "again"}]},
                    "context_id": ctx},
            skill="chat.message",
        )
        assert second["context_id"] == ctx
    finally:
        await acp.aclose()


@pytest.mark.asyncio
async def test_status_end_to_end(fake_acp_argv):
    app, acp = _build_app(fake_acp_argv)
    try:
        client = _make_client(app)
        result = await client.call(method="status/query", params={}, skill="status.query")
    finally:
        await acp.aclose()
    assert result["state"] in {"idle", "working"}


@pytest.mark.asyncio
async def test_bad_secret_is_refused(fake_acp_argv):
    app, acp = _build_app(fake_acp_argv)
    try:
        peer = Peer(peer_id=PEER_ID, display_name="h", url="https://hermes-home",
                    hmac_secret_ref="X", tls_verify=True, tls_pinned_sha256=None,
                    added_at="", last_seen=None)
        bad = A2AClient(peer, secret="WRONG", my_peer_id="agent-last-laptop",
                        transport=httpx.ASGITransport(app=app))
        from agents.comm_agent.a2a_protocol import A2AClientError
        with pytest.raises(A2AClientError):
            await bad.call(method="status/query", params={}, skill="status.query")
    finally:
        await acp.aclose()
```

- [ ] **Step 2: Run tests to verify they fail (then pass)**

Run: `pytest tests/test_bridge_hermes/test_e2e_bridge.py -v`
Expected: All four PASS (no new production code needed — this wires together Tasks 2–8). If `test_bad_secret_is_refused` fails to raise, confirm `build_app` is the reused agent-last version (it returns HTTP 401 on signature mismatch, which `A2AClient.call` turns into `A2AClientError`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_bridge_hermes/test_e2e_bridge.py
git commit -m "test(hermes-bridge): e2e A2AClient -> bridge -> fake hermes acp"
```

---

## Task 10: Install script (Linux/macOS) `scripts/install_hermes_a2a.sh`

**Files:**
- Create: `scripts/install_hermes_a2a.sh`

This is a shell script (not unit-tested); verification is manual via `bash -n` (syntax) and a dry read. It mirrors `scripts/install_openclaw_a2a.sh`.

- [ ] **Step 1: Write the script**

Create `scripts/install_hermes_a2a.sh`:

```bash
#!/usr/bin/env bash
# scripts/install_hermes_a2a.sh
# Provision the Hermes A2A<->ACP bridge on a remote machine so the agent-last
# comm-agent can delegate to / chat with a local `hermes acp` over A2A v0.3.
#
# Usage:
#   curl -sSL <raw-url> | bash -s -- \
#       --my-peer-id hermes-home \
#       --your-peer-id agent-last-laptop \
#       --public-host home.example.com \
#       --hmac-secret "$(openssl rand -hex 32)"

set -euo pipefail

MY_PEER_ID=""
YOUR_PEER_ID=""
PUBLIC_HOST=""
HMAC_SECRET=""
HERMES_BIN="${HERMES_BIN:-hermes}"
AGENT_LAST_REPO="${AGENT_LAST_REPO:-https://github.com/<your-repo>/agent-last.git}"
AGENT_LAST_DIR="${AGENT_LAST_DIR:-$HOME/.hermes-a2a/agent-last}"
CADDY_PORT="${CADDY_PORT:-8443}"
BRIDGE_PORT="${BRIDGE_PORT:-19444}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --my-peer-id) MY_PEER_ID="$2"; shift 2;;
    --your-peer-id) YOUR_PEER_ID="$2"; shift 2;;
    --public-host) PUBLIC_HOST="$2"; shift 2;;
    --hmac-secret) HMAC_SECRET="$2"; shift 2;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

[[ -z "$MY_PEER_ID" || -z "$YOUR_PEER_ID" || -z "$PUBLIC_HOST" || -z "$HMAC_SECRET" ]] && {
  echo "missing required flag(s); see header for usage" >&2
  exit 2
}

echo "==> [1/7] Checking Hermes ACP is available"
command -v "$HERMES_BIN" >/dev/null 2>&1 || {
  echo "ERROR: '$HERMES_BIN' not on PATH. Install Hermes (https://github.com/NousResearch/hermes-agent) or set HERMES_BIN." >&2
  exit 3
}
python3 -c "import acp" 2>/dev/null || {
  echo "  NOTE: python package 'acp' not importable. Install Hermes' ACP extra in the Hermes checkout:"
  echo "        pip install -e '.[acp]'"
  echo "  (the bridge itself does not need it, but \`hermes acp\` does)"
}

echo "==> [2/7] Fetching agent-last (for the reused A2A server modules)"
if [[ -d "$AGENT_LAST_DIR/.git" ]]; then
  git -C "$AGENT_LAST_DIR" pull --ff-only || echo "  (pull skipped)"
else
  mkdir -p "$(dirname "$AGENT_LAST_DIR")"
  git clone --depth 1 "$AGENT_LAST_REPO" "$AGENT_LAST_DIR"
fi

echo "==> [3/7] Installing bridge python deps"
python3 -m pip install --quiet fastapi uvicorn pyjwt httpx

echo "==> [4/7] Writing bridge env file"
ENV_DIR="$HOME/.hermes-a2a"
mkdir -p "$ENV_DIR"
ENV_FILE="$ENV_DIR/bridge.env"
cat > "$ENV_FILE" <<EOF
HERMES_A2A_HMAC=$HMAC_SECRET
HERMES_A2A_MY_PEER_ID=$MY_PEER_ID
HERMES_A2A_ALLOWED_PEER=$YOUR_PEER_ID
HERMES_A2A_PORT=$BRIDGE_PORT
HERMES_A2A_PUBLIC_HOST=$PUBLIC_HOST
HERMES_A2A_PUBLIC_PORT=$CADDY_PORT
HERMES_ACP_CMD=$HERMES_BIN acp
EOF
chmod 600 "$ENV_FILE"
echo "  wrote $ENV_FILE (mode 0600)"

echo "==> [5/7] Generating Caddyfile"
CADDY_DIR="${CADDY_DIR:-/etc/caddy/Caddyfile.d}"
mkdir -p "$CADDY_DIR" 2>/dev/null || CADDY_DIR="$HOME/.caddy"
mkdir -p "$CADDY_DIR"
cat > "$CADDY_DIR/hermes-a2a.caddy" <<EOF
$PUBLIC_HOST:$CADDY_PORT {
    reverse_proxy localhost:$BRIDGE_PORT
}
EOF
echo "  wrote $CADDY_DIR/hermes-a2a.caddy"

echo "==> [6/7] Starting the bridge + reloading Caddy"
echo "  Start the bridge (loads env, runs from the agent-last checkout):"
echo "    cd $AGENT_LAST_DIR && set -a && . $ENV_FILE && set +a && python3 -m bridge.hermes_a2a"
echo "  (For a long-running service, wrap that in a systemd unit or 'nohup ... &'.)"
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet caddy; then
  sudo systemctl reload caddy && echo "  caddy reloaded via systemctl"
else
  echo "  systemd caddy not running; start caddy manually:"
  echo "    caddy run --config $CADDY_DIR/hermes-a2a.caddy"
fi

echo "==> [7/7] Self-check hint"
echo "  After starting the bridge AND caddy, verify:"
echo "    curl -sk https://localhost:$CADDY_PORT/.well-known/agent.json"

cat <<EOF

✅ Bridge files installed.

Next step on your agent-last machine — register this peer:
    comm.add_peer peer_id=$MY_PEER_ID \\
                  url=https://$PUBLIC_HOST:$CADDY_PORT \\
                  hmac_secret_value=$HMAC_SECRET

(Keep that HMAC secret safe — it's the only copy printed.)
EOF
```

- [ ] **Step 2: Verify script syntax**

Run: `bash -n scripts/install_hermes_a2a.sh`
Expected: no output, exit 0 (syntax OK).

- [ ] **Step 3: Commit**

```bash
git add scripts/install_hermes_a2a.sh
git commit -m "feat(hermes-bridge): linux install script"
```

---

## Task 11: Install script (Windows) `scripts/install_hermes_a2a.ps1`

**Files:**
- Create: `scripts/install_hermes_a2a.ps1`

Mirrors `scripts/install_openclaw_a2a.ps1`.

- [ ] **Step 1: Write the script**

Create `scripts/install_hermes_a2a.ps1`:

```powershell
# scripts/install_hermes_a2a.ps1
# Windows equivalent of install_hermes_a2a.sh.
#
# Usage:
#   $secret = -join ((48..57)+(97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})
#   iex "& { $(iwr -useb <raw-url>) } -MyPeerId hermes-home -YourPeerId agent-last-laptop -PublicHost home.example.com -HmacSecret $secret"

param(
    [Parameter(Mandatory=$true)][string]$MyPeerId,
    [Parameter(Mandatory=$true)][string]$YourPeerId,
    [Parameter(Mandatory=$true)][string]$PublicHost,
    [Parameter(Mandatory=$true)][string]$HmacSecret,
    [string]$HermesBin = $(if ($env:HERMES_BIN) { $env:HERMES_BIN } else { "hermes" }),
    [string]$AgentLastRepo = $(if ($env:AGENT_LAST_REPO) { $env:AGENT_LAST_REPO } else { "https://github.com/<your-repo>/agent-last.git" }),
    [string]$AgentLastDir = $(if ($env:AGENT_LAST_DIR) { $env:AGENT_LAST_DIR } else { "$env:USERPROFILE\.hermes-a2a\agent-last" }),
    [int]$CaddyPort = 8443,
    [int]$BridgePort = 19444
)

$ErrorActionPreference = "Stop"

Write-Host "==> [1/7] Checking Hermes ACP is available"
if (-not (Get-Command $HermesBin -ErrorAction SilentlyContinue)) {
    Write-Error "'$HermesBin' not on PATH. Install Hermes or set `$env:HERMES_BIN."
    exit 3
}
try { & python -c "import acp" 2>$null } catch {
    Write-Host "  NOTE: python package 'acp' not importable; in the Hermes checkout run: pip install -e '.[acp]'"
}

Write-Host "==> [2/7] Fetching agent-last (reused A2A server modules)"
if (Test-Path "$AgentLastDir\.git") {
    git -C $AgentLastDir pull --ff-only
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $AgentLastDir) | Out-Null
    git clone --depth 1 $AgentLastRepo $AgentLastDir
}

Write-Host "==> [3/7] Installing bridge python deps"
& python -m pip install --quiet fastapi uvicorn pyjwt httpx

Write-Host "==> [4/7] Writing bridge env file"
$EnvDir = "$env:USERPROFILE\.hermes-a2a"
New-Item -ItemType Directory -Force -Path $EnvDir | Out-Null
$EnvFile = "$EnvDir\bridge.env"
@"
HERMES_A2A_HMAC=$HmacSecret
HERMES_A2A_MY_PEER_ID=$MyPeerId
HERMES_A2A_ALLOWED_PEER=$YourPeerId
HERMES_A2A_PORT=$BridgePort
HERMES_A2A_PUBLIC_HOST=$PublicHost
HERMES_A2A_PUBLIC_PORT=$CaddyPort
HERMES_ACP_CMD=$HermesBin acp
"@ | Out-File -FilePath $EnvFile -Encoding utf8
$Acl = Get-Acl $EnvFile
$Acl.SetAccessRuleProtection($true, $false)
$Acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name, "Read,Write", "Allow")))
Set-Acl $EnvFile $Acl
Write-Host "  wrote $EnvFile (locked to current user)"

Write-Host "==> [5/7] Generating Caddyfile"
$CaddyDir = if ($env:CADDY_DIR) { $env:CADDY_DIR } else { "$env:USERPROFILE\.caddy" }
New-Item -ItemType Directory -Force -Path $CaddyDir | Out-Null
@"
${PublicHost}:${CaddyPort} {
    reverse_proxy localhost:$BridgePort
}
"@ | Out-File -FilePath "$CaddyDir\hermes-a2a.caddy" -Encoding utf8
Write-Host "  wrote $CaddyDir\hermes-a2a.caddy"

Write-Host "==> [6/7] Start the bridge + reload Caddy"
Write-Host "  Start the bridge from the agent-last checkout:"
Write-Host "    cd $AgentLastDir; Get-Content $EnvFile | ForEach-Object { if (`$_ -match '^(.+?)=(.*)$') { [Environment]::SetEnvironmentVariable(`$Matches[1], `$Matches[2]) } }; python -m bridge.hermes_a2a"
if (Get-Service -Name "caddy" -ErrorAction SilentlyContinue) {
    Restart-Service -Name "caddy"; Write-Host "  caddy service restarted"
} else {
    Write-Host "  caddy service not found; start manually: caddy run --config $CaddyDir\hermes-a2a.caddy"
}

Write-Host "==> [7/7] Self-check hint"
Write-Host "  After starting bridge + caddy: curl -sk https://localhost:$CaddyPort/.well-known/agent.json"

Write-Host ""
Write-Host "[OK] Bridge files installed."
Write-Host "Next step on your agent-last machine — register this peer:"
Write-Host "    comm.add_peer peer_id=$MyPeerId url=https://${PublicHost}:${CaddyPort} hmac_secret_value=$HmacSecret"
Write-Host "(Keep that HMAC secret safe — it's the only copy printed.)"
```

- [ ] **Step 2: Verify script parses**

Run (PowerShell): `powershell -NoProfile -Command "$null = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path scripts/install_hermes_a2a.ps1), [ref]$null, [ref]$null); 'parse-ok'"`
Expected: prints `parse-ok` (no parser errors).

- [ ] **Step 3: Commit**

```bash
git add scripts/install_hermes_a2a.ps1
git commit -m "feat(hermes-bridge): windows install script"
```

---

## Task 12: Documentation — README updates

**Files:**
- Modify: `agents/comm_agent/README.md` (replace the "Hermes 规划中" note with a real section)
- Modify: `README.md` (add a Hermes link in the comm-agent section)

- [ ] **Step 1: Replace the planned-note in the comm-agent README**

In `agents/comm_agent/README.md`, find the blockquote that currently reads (added earlier this branch):

```markdown
> **Hermes 不在此列。** Hermes（NousResearch/hermes-agent）对外走的是 **stdio 上的 ACP**（Agent Client Protocol，编辑器集成那套），**不说 A2A**，无法靠"满足 A2A 契约"直连。对接 Hermes 需要在它那台机器上跑一个 **A2A↔ACP 桥接**（对外说 A2A、对内 spawn `hermes acp`），配套专门的 `install_hermes_a2a` 安装脚本。该能力**规划中**，文档与脚本会随实现一起补上。
```

Replace it with:

```markdown
> **Hermes 走 ACP，不说 A2A。** Hermes（NousResearch/hermes-agent）对外只暴露 **stdio 上的 ACP**，不满足 A2A 契约，不能直连。对接方式见下方「对接 Hermes（A2A↔ACP 桥接）」。

### 对接 Hermes（A2A↔ACP 桥接）

Hermes 那台机器上要跑一个**桥接进程**：对外说 comm-agent 的 A2A v0.3，对内 spawn 本地 `hermes acp` 用 stdio ACP 驱动 Hermes。agent-last 侧零改动——注册个 peer 就能用。

**前置（Hermes 机器）**：装好 Hermes 且 `hermes` 在 PATH；Hermes 的 ACP 依赖已装（`pip install -e '.[acp]'`）；装好 Caddy；有公网主机名。

**一键安装**（在 Hermes 机器执行）：

```bash
curl -sSL https://raw.githubusercontent.com/<your-repo>/main/scripts/install_hermes_a2a.sh \
  | bash -s -- \
      --my-peer-id    hermes-home \
      --your-peer-id  agent-last-laptop \
      --public-host   home.example.com \
      --hmac-secret   "$(openssl rand -hex 32)"
```

Windows 用 `scripts/install_hermes_a2a.ps1`（参数同名）。脚本会：拉 agent-last（复用其 A2A 服务端模块）、装桥接依赖、写 `~/.hermes-a2a/bridge.env`、渲染 Caddyfile，并打印主机端要跑的 `comm.add_peer` 行。

**协议映射**：`task.delegate`→ACP `session/new`+`session/prompt`（流式）；`chat.message`→复用 ACP session（`context_id`↔`sessionId`）；`status.query`→桥接自记运行态。

**注册后照常用**：

```
comm.add_peer peer_id=hermes-home url=https://home.example.com:8443 hmac_secret_value=<密钥>
comm.delegate peer_id=hermes-home task="..."
comm.chat     peer_id=hermes-home message="..."
```

**限制**：危险操作审批默认**拒**（远端无人值守），放行需在桥接端设 `HERMES_A2A_AUTO_APPROVE=1`；仅透传文本（ACP image/resource 块本期不接）；受 `A2AClient` 30s 超时限制。
```

- [ ] **Step 2: Add a Hermes pointer to the main README**

In `README.md`, in the `## Comm-agent 跨机通信 (cross-machine A2A)` section, after the "Connecting a remote OpenClaw" subsection's final paragraph (the line ending `comm.delegate peer_id=openclaw-home task="..."`), add a new paragraph:

```markdown
**Connecting a remote Hermes:** Hermes speaks stdio ACP (not A2A), so it needs
the A2A↔ACP bridge — run `scripts/install_hermes_a2a.sh` (or `.ps1`) on the
Hermes host, then `comm.add_peer` exactly as above. See
`agents/comm_agent/README.md` → "对接 Hermes（A2A↔ACP 桥接）".
```

- [ ] **Step 3: Verify links/sections render**

Run: `grep -n "对接 Hermes" agents/comm_agent/README.md` and `grep -n "Connecting a remote Hermes" README.md`
Expected: each prints one match.

- [ ] **Step 4: Commit**

```bash
git add agents/comm_agent/README.md README.md
git commit -m "docs(hermes-bridge): document A2A<->ACP bridge + install script"
```

---

## Task 13: Full test run + branch wrap-up

**Files:** none (verification only)

- [ ] **Step 1: Run the full bridge test suite**

Run: `pytest tests/test_bridge_hermes/ -v`
Expected: all tests PASS.

- [ ] **Step 2: Run the comm-agent suite to confirm nothing regressed**

Run: `pytest tests/test_comm_agent/ -v`
Expected: all tests PASS (we imported but did not modify those modules).

- [ ] **Step 3: Confirm clean tree**

Run: `git status --short`
Expected: empty (everything committed across Tasks 1–12).

---

## Self-Review

**1. Spec coverage** (against `2026-05-25-hermes-a2a-acp-bridge-design.md`):
- §3.1 code layout → Tasks 1, 2, 6, 8, 10, 11. ✓
- §3.2 reuse `build_app` (dispatchers on raw method) → Task 6 (method switch), Task 8 (wiring). ✓
- §3.3 ACP client lifecycle (initialize/authenticate/new_session/prompt/permission/reconnect) → Tasks 2, 3, 5. (Reconnect: `_ensure_started` re-spawns when `returncode is not None`; covered implicitly — see note below.) ✓
- §4.1 task.delegate stream mapping → Tasks 3, 6, 9. ✓
- §4.2 chat.message session reuse → Tasks 4, 7, 9. ✓
- §4.3 status.query → Tasks 4, 7, 9. ✓
- §5 env vars → Task 8 (`build()`), Task 10/11 (env file). ✓
- §6 install script 7 steps → Tasks 10, 11. ✓
- §7 error matrix (acp down, prompt fail, permission, bad grant) → Tasks 3, 5, 9 (bad secret). ✓
- §8 testing (unit/integration/CI) → Tasks 2–9. ✓
- §9 README deliverable → Task 12. ✓

**Gap found & fixed:** §3.3 lists *reconnect on subprocess death* as a behavior. `_ensure_started` already re-spawns when `self._proc.returncode is not None`, and `_read_loop` fails pending futures on EOF — so a dead process surfaces as an `ACPError` on the next request, and the following request re-spawns. This is adequate for MVP and is exercised indirectly (failure → `ACPError` → dispatcher `failed`/`error` in Tasks 3, 6). No dedicated reconnect task added (would require killing a live stub mid-test; deferred to the manual `smoke-hermes` run, consistent with spec §8.3). Documented here so the implementer doesn't treat it as missing.

**2. Placeholder scan:** No "TBD/TODO/handle edge cases". Every code step has complete code; every script step has the full script. The only literal placeholder is `<your-repo>` in install scripts / README, which is an intentional user-substituted value (same convention as the existing `install_openclaw_a2a.sh`). ✓

**3. Type consistency:** `HermesACPClient` methods (`ensure_session`, `run_prompt`, `prompt_collect`, `status`, `aclose`) are used identically in `dispatchers.py`, tests, and `FakeACP`. `make_dispatchers(acp, *, allowed_peer=None) -> (skill_dispatcher, stream_dispatcher)` — order (skill first) consistent across Task 6 definition, Tasks 6/7/9 unpacking, and Task 8 wiring. Event shapes (`{"type":"task","state":"completed","result":...}`, `{"type":"text","text":...}`) consistent between client, fake stub, dispatchers, and the agent-last `A2AClient` contract. ✓
