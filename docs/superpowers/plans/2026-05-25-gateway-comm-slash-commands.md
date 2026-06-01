# Gateway comm slash commands (`/chat` / `/task`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let whitelisted QQ / Feishu users drive remote A2A peers from chat via `/task <peer_id> <task>`, `/chat <peer_id> <message>`, `/peers`, and `/help`.

**Architecture:** A UI-free `gateway/slash.py` parses leading-slash messages and dispatches to the existing `comm.*` MCP tools on the per-turn `MCPHost` that `gateway.runner` already builds, returning a plain-text reply. `gateway.runner._run_turn_locked` calls it right after bootstrap; a string reply short-circuits the planner, `None` falls through to normal chat. A per-platform `allowed_users` allowlist in `gateways.json` gates access (empty = deny). `/chat` context ids persist per `(session_key, peer_id)` in a small JSON file.

**Tech Stack:** Python 3.13, pytest, existing comm-agent A2A tools, `gateway.credentials`, `agent_paths`.

**Spec:** `docs/superpowers/specs/2026-05-25-gateway-comm-slash-commands-design.md`

---

## File Structure

- **Create** `gateway/slash.py` — slash parsing, allowlist gate, comm dispatch, chat-context store. One responsibility: turn a `/command` line into a reply string (or `None`).
- **Modify** `gateway/runner.py` — call `handle_slash` after `_bootstrap` in `_run_turn_locked`; skip session-history append for slash commands.
- **Modify** `orchestrator/repl_commands.py` — add optional `allowed_users` field to the `/gateway setup` wizard (`_gw_field_specs`, `_gw_fields`).
- **Create** `tests/test_gateway/test_slash.py` — unit tests with a fake host.
- **Modify** `README.md` — document the chat-gateway slash commands + allowlist.

**Allowlist storage decision:** `allowed_users` is stored as a **comma-separated string** in `gateways.json` (e.g. `"ou_a,ou_b"`). This keeps the existing string-only setup wizard unchanged. `gateway/slash._allowed_users` also accepts a JSON list, so a hand-edited `["ou_a","ou_b"]` works too.

---

## Task 1: Authorization helpers (`gateway/slash.py`)

**Files:**
- Create: `gateway/slash.py`
- Test: `tests/test_gateway/test_slash.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gateway/test_slash.py
from __future__ import annotations

import json

import pytest

from gateway import slash


def test_platform_from_session_key():
    assert slash._platform_from_session_key("qq:123") == "qq"
    assert slash._platform_from_session_key("feishu:abc") == "feishu"
    assert slash._platform_from_session_key("") == ""
    assert slash._platform_from_session_key("nokey") == ""


def test_is_authorized_reads_allowlist(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("qq", {"app_id": "x", "allowed_users": "ou_a,ou_b"})
    assert slash._is_authorized("qq:123", "ou_a") is True
    assert slash._is_authorized("qq:123", "ou_b") is True
    assert slash._is_authorized("qq:123", "ou_other") is False


def test_is_authorized_empty_allowlist_denies(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("qq", {"app_id": "x"})  # no allowed_users
    assert slash._is_authorized("qq:123", "ou_a") is False


def test_is_authorized_no_user_denies(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("qq", {"allowed_users": "ou_a"})
    assert slash._is_authorized("qq:123", "") is False


def test_is_authorized_accepts_json_list(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("feishu", {"allowed_users": ["ou_a", "ou_b"]})
    assert slash._is_authorized("feishu:c", "ou_b") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gateway/test_slash.py -p no:cacheprovider -q`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'gateway.slash'`.

- [ ] **Step 3: Write minimal implementation**

```python
# gateway/slash.py
"""Slash commands for chat-platform gateways (QQ / Feishu).

Whitelisted users drive remote A2A peers from chat:
  /task <peer_id> <task>     one-shot delegation (comm.delegate)
  /chat <peer_id> <message>  multi-turn conversation (comm.chat, context kept)
  /peers                     list registered peer_ids
  /help                      usage

``handle_slash`` returns the reply STRING for a handled command, or ``None`` to
fall through to the normal planner path (non-slash input, or an unrecognized
/command). UI-free on purpose: the REPL's ReplCommandHandler is coupled to Rich
rendering and an in-memory current-peer; the gateway runs one isolated turn per
message and needs a plain-text reply.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gateway import credentials as gw_creds

COMM_AGENT_ID = "comm-agent"
_RECOGNIZED = {"/task", "/chat", "/peers", "/help"}


def _platform_from_session_key(session_key: str) -> str:
    """``qq:123`` -> ``qq``; ``feishu:abc`` -> ``feishu``; no prefix -> ``""``."""
    if not session_key or ":" not in session_key:
        return ""
    return session_key.split(":", 1)[0]


def _allowed_users(platform: str) -> list[str]:
    """Read the per-platform allowlist from gateways.json.

    Accepts either a comma-separated string (what the setup wizard writes) or a
    JSON list (a hand-edited gateways.json). Empty / missing -> ``[]``.
    """
    if not platform:
        return []
    users = gw_creds.load(platform).get("allowed_users") or []
    if isinstance(users, str):
        users = [u.strip() for u in users.split(",") if u.strip()]
    return [str(u) for u in users]


def _is_authorized(session_key: str, user_id: str) -> bool:
    """Fail-safe: no user id, or empty/missing allowlist, denies."""
    if not user_id:
        return False
    return user_id in _allowed_users(_platform_from_session_key(session_key))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gateway/test_slash.py -p no:cacheprovider -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add gateway/slash.py tests/test_gateway/test_slash.py
git commit -m "feat(gateway-slash): platform + allowlist auth helpers"
```

---

## Task 2: `handle_slash` routing, fall-through, and auth gate

**Files:**
- Modify: `gateway/slash.py`
- Test: `tests/test_gateway/test_slash.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gateway/test_slash.py`:

```python
class _FakeHost:
    """Stands in for MCPHost.call_tool; returns canned comm.* JSON envelopes."""

    def __init__(self, responses: dict[str, dict] | None = None):
        self._responses = responses or {}
        self.calls: list[tuple[str, str, dict]] = []

    async def call_tool(self, agent_id: str, name: str, arguments: dict):
        self.calls.append((agent_id, name, arguments))
        payload = self._responses.get(name, {"ok": True})
        return {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        }


def _authorize(tmp_config_dir, platform="qq", user="ou_a"):
    from gateway import credentials as gw_creds
    gw_creds.save(platform, {"allowed_users": user})


@pytest.mark.asyncio
async def test_non_slash_returns_none(tmp_config_dir):
    host = _FakeHost()
    assert await slash.handle_slash("你好", host=host, session_key="qq:1", user_id="ou_a") is None
    assert host.calls == []


@pytest.mark.asyncio
async def test_unknown_command_returns_none(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost()
    assert await slash.handle_slash("/wat now", host=host, session_key="qq:1", user_id="ou_a") is None
    assert host.calls == []


@pytest.mark.asyncio
async def test_unauthorized_user_refused(tmp_config_dir):
    _authorize(tmp_config_dir, user="ou_owner")
    host = _FakeHost()
    reply = await slash.handle_slash("/peers", host=host, session_key="qq:1", user_id="ou_intruder")
    assert reply is not None
    assert "权限" in reply
    assert host.calls == []  # no comm tool touched


@pytest.mark.asyncio
async def test_help_lists_commands(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost()
    reply = await slash.handle_slash("/help", host=host, session_key="qq:1", user_id="ou_a")
    assert "/task" in reply and "/chat" in reply and "/peers" in reply


@pytest.mark.asyncio
async def test_peers_lists_registered(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost({"comm.list_peers": {"peers": [
        {"peer_id": "openclaw-home", "display_name": "Home box"},
    ]}})
    reply = await slash.handle_slash("/peers", host=host, session_key="qq:1", user_id="ou_a")
    assert "openclaw-home" in reply
    assert host.calls[0][1] == "comm.list_peers"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gateway/test_slash.py -p no:cacheprovider -q -k "non_slash or unknown_command or unauthorized or help_lists or peers_lists"`
Expected: FAIL — `AttributeError: module 'gateway.slash' has no attribute 'handle_slash'`.

- [ ] **Step 3: Write minimal implementation**

Append to `gateway/slash.py`:

```python
def _unwrap(result: Any) -> tuple[bool, str]:
    """Normalize call_tool result into (is_error, text). Mirrors the REPL handler."""
    try:
        is_error = bool(getattr(result, "isError", False))
        content = getattr(result, "content", None)
        if content and hasattr(content[0], "text"):
            return is_error, content[0].text
    except (IndexError, TypeError, AttributeError):
        pass
    try:
        is_error = bool(result.get("isError", False))
        content = result.get("content", [])
        if content:
            return is_error, content[0].get("text", "")
    except (AttributeError, IndexError, TypeError):
        pass
    return True, "unexpected call_tool result format"


async def _call_comm(host, tool: str, args: dict) -> tuple[bool, dict]:
    """Call a comm.* tool; return (ok, data). data carries {'error': ...} on failure."""
    result = await host.call_tool(COMM_AGENT_ID, tool, args)
    is_error, text = _unwrap(result)
    if is_error:
        return False, {"error": text or "comm-agent unavailable"}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False, {"error": f"invalid comm response: {text!r}"}
    if not data.get("ok", True):
        return False, {"error": data.get("error", str(data))}
    return True, data


_USAGE = (
    "可用命令:\n"
    "/task <peer_id> <任务>  — 委托一次性任务给远程 peer\n"
    "/chat <peer_id> <消息>  — 与远程 peer 多轮对话\n"
    "/peers                  — 列出已注册的 peer\n"
    "/help                   — 显示本帮助"
)


async def _do_peers(host) -> str:
    ok, data = await _call_comm(host, "comm.list_peers", {})
    if not ok:
        return f"获取 peer 列表失败:{data.get('error')}"
    peers = data.get("peers", [])
    if not peers:
        return "还没有注册任何 peer。(在 REPL 里用 /comm add 添加)"
    lines = ["已注册的 peer:"]
    for p in peers:
        lines.append(f"- {p.get('peer_id', '')} — {p.get('display_name', '')}")
    return "\n".join(lines)


async def handle_slash(line: str, *, host, session_key: str, user_id: str) -> str | None:
    """Dispatch a chat-platform slash command. See module docstring for contract."""
    line = (line or "").strip()
    if not line.startswith("/"):
        return None
    parts = line.split(maxsplit=2)
    command = parts[0].lower()
    if command not in _RECOGNIZED:
        return None  # unknown slash -> planner fall-through (today's behaviour)

    if not _is_authorized(session_key, user_id):
        return (
            "抱歉,你没有权限使用这个命令。"
            "(管理员可在 /gateway setup 的 allowed_users 里添加你的 user_id)"
        )

    if command == "/help":
        return _USAGE
    if command == "/peers":
        return await _do_peers(host)
    if command == "/task":
        return await _do_task(host, parts)
    if command == "/chat":
        return await _do_chat(host, parts, session_key)
    return None  # unreachable (command in _RECOGNIZED)
```

Note: `_do_task` and `_do_chat` are added in Tasks 3–4. To keep this task's tests green, add temporary stubs now (they will be replaced):

```python
async def _do_task(host, parts: list[str]) -> str:  # replaced in Task 3
    return "(task not implemented)"


async def _do_chat(host, parts: list[str], session_key: str) -> str:  # replaced in Task 4
    return "(chat not implemented)"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gateway/test_slash.py -p no:cacheprovider -q`
Expected: PASS (all tasks-1 and tasks-2 tests).

- [ ] **Step 5: Commit**

```bash
git add gateway/slash.py tests/test_gateway/test_slash.py
git commit -m "feat(gateway-slash): handle_slash routing, auth gate, /peers, /help"
```

---

## Task 3: `/task` delegation

**Files:**
- Modify: `gateway/slash.py` (replace the `_do_task` stub)
- Test: `tests/test_gateway/test_slash.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gateway/test_slash.py`:

```python
@pytest.mark.asyncio
async def test_task_delegates_and_renders_result(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost({"comm.delegate": {"final_result": "已总结:3 个要点", "events_count": 4}})
    reply = await slash.handle_slash(
        "/task openclaw-home 总结 ~/notes.md", host=host, session_key="qq:1", user_id="ou_a",
    )
    agent_id, tool, args = host.calls[0]
    assert tool == "comm.delegate"
    assert args["peer_id"] == "openclaw-home"
    assert args["task"] == "总结 ~/notes.md"
    assert args["stream"] is False
    assert "已总结:3 个要点" in reply


@pytest.mark.asyncio
async def test_task_renders_parts_dict_result(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost({"comm.delegate": {"final_result": {"parts": [{"text": "part-A"}, {"text": "part-B"}]}}})
    reply = await slash.handle_slash(
        "/task p hello", host=host, session_key="qq:1", user_id="ou_a",
    )
    assert "part-A" in reply and "part-B" in reply


@pytest.mark.asyncio
async def test_task_missing_args_shows_usage(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost()
    reply = await slash.handle_slash("/task openclaw-home", host=host, session_key="qq:1", user_id="ou_a")
    assert "用法" in reply
    assert host.calls == []


@pytest.mark.asyncio
async def test_task_surfaces_comm_error(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost({"comm.delegate": {"ok": False, "error": "unknown peer 'p'"}})
    reply = await slash.handle_slash("/task p do it", host=host, session_key="qq:1", user_id="ou_a")
    assert "unknown peer" in reply
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gateway/test_slash.py -p no:cacheprovider -q -k task`
Expected: FAIL — reply is `"(task not implemented)"`, assertions on result text fail.

- [ ] **Step 3: Write minimal implementation**

In `gateway/slash.py`, replace the `_do_task` stub with:

```python
def _render_final(final: Any) -> str:
    """comm.delegate final_result may be a dict with A2A parts, a str, or None."""
    if isinstance(final, dict):
        parts_list = final.get("parts", [])
        joined = "\n".join(
            p.get("text", "") for p in parts_list
            if isinstance(p, dict) and p.get("text")
        )
        return joined or json.dumps(final, ensure_ascii=False)
    if final is None:
        return "(无结果)"
    return str(final)


async def _do_task(host, parts: list[str]) -> str:
    if len(parts) < 3 or not parts[2].strip():
        return "用法:/task <peer_id> <任务>"
    peer_id, task = parts[1], parts[2]
    ok, data = await _call_comm(host, "comm.delegate", {
        "peer_id": peer_id, "task": task, "stream": False,
    })
    if not ok:
        return f"委托失败:{data.get('error')}"
    return f"[{peer_id}] {_render_final(data.get('final_result'))}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gateway/test_slash.py -p no:cacheprovider -q`
Expected: PASS (all tests so far).

- [ ] **Step 5: Commit**

```bash
git add gateway/slash.py tests/test_gateway/test_slash.py
git commit -m "feat(gateway-slash): /task delegation via comm.delegate"
```

---

## Task 4: `/chat` with persisted multi-turn context

**Files:**
- Modify: `gateway/slash.py` (replace the `_do_chat` stub; add context store)
- Test: `tests/test_gateway/test_slash.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gateway/test_slash.py`:

```python
@pytest.mark.asyncio
async def test_chat_replies_and_persists_context(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost({"comm.chat": {"reply": "你好呀", "context_id": "ctx-1"}})
    reply = await slash.handle_slash(
        "/chat openclaw-home 在吗", host=host, session_key="qq:1", user_id="ou_a",
    )
    _agent, tool, args = host.calls[0]
    assert tool == "comm.chat"
    assert args["peer_id"] == "openclaw-home"
    assert args["message"] == "在吗"
    assert args["context_id"] is None  # first turn: no prior context
    assert "你好呀" in reply
    # context_id was persisted
    assert slash._load_chat_context("qq:1", "openclaw-home") == "ctx-1"


@pytest.mark.asyncio
async def test_chat_reuses_saved_context(tmp_config_dir):
    _authorize(tmp_config_dir)
    slash._save_chat_context("qq:1", "openclaw-home", "ctx-existing")
    host = _FakeHost({"comm.chat": {"reply": "继续", "context_id": "ctx-existing"}})
    await slash.handle_slash(
        "/chat openclaw-home 接着聊", host=host, session_key="qq:1", user_id="ou_a",
    )
    assert host.calls[0][2]["context_id"] == "ctx-existing"


@pytest.mark.asyncio
async def test_chat_context_isolated_per_session_and_peer(tmp_config_dir):
    slash._save_chat_context("qq:1", "peerA", "ctxA")
    assert slash._load_chat_context("qq:1", "peerB") is None
    assert slash._load_chat_context("qq:2", "peerA") is None


@pytest.mark.asyncio
async def test_chat_missing_args_shows_usage(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost()
    reply = await slash.handle_slash("/chat openclaw-home", host=host, session_key="qq:1", user_id="ou_a")
    assert "用法" in reply
    assert host.calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gateway/test_slash.py -p no:cacheprovider -q -k chat`
Expected: FAIL — `AttributeError: module 'gateway.slash' has no attribute '_save_chat_context'` and the `_do_chat` stub returns the placeholder.

- [ ] **Step 3: Write minimal implementation**

In `gateway/slash.py`, add the context store and replace the `_do_chat` stub:

```python
def _context_store_path() -> Path:
    from agent_paths import config_dir
    return config_dir() / "comm_chat_contexts.json"


def _load_chat_context(session_key: str, peer_id: str) -> str | None:
    p = _context_store_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get(f"{session_key}::{peer_id}")


def _save_chat_context(session_key: str, peer_id: str, context_id: str) -> None:
    p = _context_store_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[f"{session_key}::{peer_id}"] = context_id
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def _do_chat(host, parts: list[str], session_key: str) -> str:
    if len(parts) < 3 or not parts[2].strip():
        return "用法:/chat <peer_id> <消息>"
    peer_id, message = parts[1], parts[2]
    ctx = _load_chat_context(session_key, peer_id)
    ok, data = await _call_comm(host, "comm.chat", {
        "peer_id": peer_id, "message": message, "context_id": ctx,
    })
    if not ok:
        return f"对话失败:{data.get('error')}"
    new_ctx = data.get("context_id")
    if new_ctx:
        _save_chat_context(session_key, peer_id, new_ctx)
    return f"[{peer_id}] {data.get('reply') or '(空回复)'}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gateway/test_slash.py -p no:cacheprovider -q`
Expected: PASS (all slash tests).

- [ ] **Step 5: Commit**

```bash
git add gateway/slash.py tests/test_gateway/test_slash.py
git commit -m "feat(gateway-slash): /chat with persisted per-(chat,peer) context"
```

---

## Task 5: Wire `handle_slash` into `gateway/runner.py`

**Files:**
- Modify: `gateway/runner.py` (`_run_turn_locked`)
- Test: `tests/test_gateway/test_slash.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gateway/test_slash.py`:

```python
@pytest.mark.asyncio
async def test_run_turn_routes_slash_and_skips_history(tmp_config_dir, monkeypatch):
    """A slash command is handled by handle_slash and NOT appended to the
    25-turn session history (it must not pollute planner context)."""
    from gateway import runner, session_store

    async def fake_bootstrap(host, router):
        return None

    async def fake_handle_slash(line, *, host, session_key, user_id):
        assert line == "/peers"
        return "SLASH_REPLY"

    monkeypatch.setattr(runner, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr("gateway.slash.handle_slash", fake_handle_slash)

    reply = await runner.run_turn(
        "/peers", session_key="qq:42", user_id="ou_a",
    )
    assert reply == "SLASH_REPLY"
    # Slash round-trips are not recorded in conversation history.
    assert session_store.load("qq:42") == []


@pytest.mark.asyncio
async def test_run_turn_non_slash_still_reaches_planner(tmp_config_dir, monkeypatch):
    """A None from handle_slash must fall through to the normal planner path."""
    from gateway import runner

    async def fake_bootstrap(host, router):
        return None

    async def fake_handle_slash(line, *, host, session_key, user_id):
        return None

    called = {"dispatch": False}

    async def fake_dispatch(**kwargs):
        called["dispatch"] = True
        return "PLANNER_REPLY"

    monkeypatch.setattr(runner, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr("gateway.slash.handle_slash", fake_handle_slash)
    monkeypatch.setattr(runner, "_build_planner", lambda router, context_text="": (lambda state: {"capability": "", "response": "PLANNER_REPLY"}))
    monkeypatch.setattr(runner, "_dispatch_decision", fake_dispatch)

    reply = await runner.run_turn("ordinary message", session_key="qq:7", user_id="ou_a")
    assert called["dispatch"] is True
    assert reply == "PLANNER_REPLY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gateway/test_slash.py -p no:cacheprovider -q -k run_turn`
Expected: FAIL — `test_run_turn_routes_slash_and_skips_history` returns the planner's reply (or errors building a real planner) because `handle_slash` is not wired in yet.

- [ ] **Step 3: Write minimal implementation**

In `gateway/runner.py`, find the `_run_turn_locked` block:

```python
    reply_text = ""
    stop_tail = None
    try:
        # CRITICAL: apply the per-user memory scope env BEFORE building the
        # planner context. ...
        _apply_memory_user_env(user_id)
        history_context, full_context = _build_planner_context(session_key)

        await _bootstrap(host, router)
        planner = _build_planner(router, context_text=full_context)
        stop_tail = await _drive_telemetry_tail(mux)
```

Replace it with (adds `is_slash_command` flag + the slash check after bootstrap):

```python
    reply_text = ""
    is_slash_command = False
    stop_tail = None
    try:
        # CRITICAL: apply the per-user memory scope env BEFORE building the
        # planner context. ...
        _apply_memory_user_env(user_id)
        history_context, full_context = _build_planner_context(session_key)

        await _bootstrap(host, router)

        # Slash commands (/task /chat /peers /help) for whitelisted users.
        # A string reply short-circuits the planner; None falls through to
        # normal chat. comm.* tools are available because _bootstrap spawned
        # the comm-agent onto this per-turn host.
        from gateway.slash import handle_slash
        slash_reply = await handle_slash(
            prompt, host=host, session_key=session_key, user_id=user_id,
        )
        if slash_reply is not None:
            is_slash_command = True
            reply_text = slash_reply
            return reply_text

        planner = _build_planner(router, context_text=full_context)
        stop_tail = await _drive_telemetry_tail(mux)
```

Then in the same function's `finally` block, find:

```python
        # Persist the turn even when the reply was an error -- a future turn
        # might still want to refer to it ("you said you couldn't do that").
        if session_key and reply_text:
            session_store.append(session_key, prompt, reply_text)
```

Replace the condition so slash commands are not recorded:

```python
        # Persist the turn even when the reply was an error -- a future turn
        # might still want to refer to it ("you said you couldn't do that").
        # Slash commands are operator actions / remote conversations, not local
        # chat, so they are deliberately excluded from the planner's history.
        if session_key and reply_text and not is_slash_command:
            session_store.append(session_key, prompt, reply_text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gateway/test_slash.py -p no:cacheprovider -q`
Expected: PASS (all slash tests including both run_turn tests).

- [ ] **Step 5: Run the full gateway suite to confirm no regression**

Run: `python -m pytest tests/test_gateway/ -p no:cacheprovider -q`
Expected: PASS (existing gateway tests + new slash tests).

- [ ] **Step 6: Commit**

```bash
git add gateway/runner.py tests/test_gateway/test_slash.py
git commit -m "feat(gateway): route /task /chat /peers /help before the planner"
```

---

## Task 6: `allowed_users` field in the `/gateway setup` wizard

**Files:**
- Modify: `orchestrator/repl_commands.py` (`_gw_field_specs`, `_gw_fields`)
- Test: `tests/test_orchestrator/test_slash_agents.py` (or a new `tests/test_orchestrator/test_gateway_fields.py`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_orchestrator/test_gateway_fields.py`:

```python
from __future__ import annotations

from orchestrator.repl_commands import ReplCommandHandler


def test_allowed_users_in_field_specs():
    for platform in ("feishu", "qq"):
        names = [spec[0] for spec in ReplCommandHandler._gw_field_specs(platform, {})]
        assert "allowed_users" in names
        # It must be optional (blank allowed) so existing setups don't break.
        spec = next(s for s in ReplCommandHandler._gw_field_specs(platform, {}) if s[0] == "allowed_users")
        assert spec[3] is True  # is_optional


def test_allowed_users_in_overview_fields():
    for platform in ("feishu", "qq"):
        assert "allowed_users" in ReplCommandHandler._gw_fields(platform)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_gateway_fields.py -p no:cacheprovider -q`
Expected: FAIL — `assert "allowed_users" in names` is False.

- [ ] **Step 3: Write minimal implementation**

In `orchestrator/repl_commands.py`, in `_gw_field_specs`, add an `allowed_users` spec to BOTH platform branches. For Feishu, add it to the base `specs` list (before the `if mode == "webhook"` block):

```python
            specs: list[tuple[str, str, bool, bool]] = [
                ("app_id", "App ID from Feishu developer console", False, False),
                ("app_secret", "App Secret", True, False),
                ("domain", "open.feishu.cn or open.larksuite.com", False, True),
                ("allowed_users", "逗号分隔的授权 open_id(可用 /chat /task;留空=无人可用)", False, True),
            ]
```

For QQ, add it to the returned list:

```python
        if platform == "qq":
            return [
                ("app_id", "QQ Bot AppID", False, False),
                ("client_secret", "QQ Bot Client Secret", True, False),
                ("intents", "Intents bitmask (blank = C2C+Group@+Channel@)", False, True),
                ("sandbox", "Use sandbox host? y/n", False, True),
                ("allowed_users", "逗号分隔的授权 openid(可用 /chat /task;留空=无人可用)", False, True),
            ]
```

In `_gw_fields`, add `"allowed_users"` to both platform lists:

```python
    @staticmethod
    def _gw_fields(platform: str) -> list[str]:
        if platform == "feishu":
            return [
                "mode", "app_id", "app_secret", "domain",
                "verify_token", "encrypt_key", "reply_in_thread", "host", "port",
                "allowed_users",
            ]
        if platform == "qq":
            return ["app_id", "client_secret", "intents", "sandbox", "allowed_users"]
        return []
```

No `_coerce_field` change is needed: `allowed_users` is stored as the raw
comma-separated string (`value.strip()` default), and `gateway/slash._allowed_users`
splits it at read time. It is not a secret, so `_gw_display` shows it verbatim.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator/test_gateway_fields.py -p no:cacheprovider -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py tests/test_orchestrator/test_gateway_fields.py
git commit -m "feat(gateway): allowed_users field in /gateway setup wizard"
```

---

## Task 7: Document in README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a subsection under the gateway section**

Find the "## 聊天平台网关 Chat Platform Gateways" section and add this subsection near its end (before "### 网关之间的设计差异" or after the QQ block):

```markdown
### 聊天里的 slash 命令(/chat /task)

白名单用户可在 QQ / 飞书里直接驱动远程 A2A peer:

| 命令 | 作用 |
|---|---|
| `/task <peer_id> <任务>` | 一次性委托(comm.delegate) |
| `/chat <peer_id> <消息>` | 多轮对话(comm.chat,按 chat+peer 保留上下文) |
| `/peers` | 列出已注册的 peer_id |
| `/help` | 用法 |

- peer 需先在 REPL 里用 `/comm add` 注册;聊天里只能**使用**,不能注册。
- **权限**:仅 `gateways.json` 里该平台 `allowed_users`(逗号分隔的 open_id / openid)中的用户可用;**留空 = 全部拒绝**(fail-safe)。在 `/gateway setup` 向导里设置。
- 非 slash 的普通消息行为不变,仍走 planner。
```

- [ ] **Step 2: Verify the section renders (visual check)**

Run: `python -c "import pathlib; print('聊天里的 slash 命令' in pathlib.Path('README.md').read_text(encoding='utf-8'))"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(gateway): document /chat /task slash commands + allowlist"
```

---

## Final verification

- [ ] **Run the full gateway + orchestrator suites:**

Run: `python -m pytest tests/test_gateway/ tests/test_orchestrator/ -p no:cacheprovider -q`
Expected: all PASS.

- [ ] **Manual smoke (optional, needs a configured peer + running gateway):**
  Configure `allowed_users` for qq, start the QQ gateway, send `/peers` from an allowed account → bot replies with the peer list; from a non-allowed account → polite refusal.
