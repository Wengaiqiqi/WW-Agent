# comm-agent 斜杠命令 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 REPL 加 `/comm`(add/list/use/rm)+ `/task` + `/chat` 斜杠命令,绕过 planner 直接驱动 comm-agent 的 `comm.*` 工具。

**Architecture:** 命令在 `orchestrator/repl_commands.py` 的 `ReplCommandHandler` 里实现;通过 `host.call_tool("comm-agent", "comm.<tool>", args)` 调子进程工具,用 `_unwrap` 归一化异构返回。"当前对端" + per-peer `context_id` 存在 handler 实例(session 内存)。底层 `comm.add_peer` 扩展 `tls_verify`/`tls_pinned_sha256` 以支持自签证书。

**Tech Stack:** Python / asyncio / rich(`Prompt`、`Console`)/ pytest / MCP(stdio）。

参考 spec:`docs/superpowers/specs/2026-05-24-comm-agent-slash-commands-design.md`

---

### Task 1: 扩展 comm.add_peer 接受 TLS 参数

**Files:**
- Modify: `agents/comm_agent/mcp_tools.py`(`add_peer` 处理器构造 `Peer` 处)
- Test: `tests/test_comm_agent/test_mcp_tools.py`

- [x] **Step 1: Write the failing tests**

追加到 `tests/test_comm_agent/test_mcp_tools.py`:

```python
@pytest.mark.asyncio
async def test_add_peer_self_signed_pins_fingerprint(reg) -> None:
    async def fake_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "schemaVersion": "0.3", "name": "lan", "description": "",
            "url": "https://192.168.1.5:8443", "version": "1.0", "skills": [],
        })
    transport = httpx.MockTransport(fake_handler)

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=lambda: transport)
    by_name = {s.name: s for s in specs}
    out = json.loads(await by_name["comm.add_peer"].handler({
        "peer_id": "lan", "url": "https://192.168.1.5:8443",
        "hmac_secret_value": "s",
        "tls_verify": False, "tls_pinned_sha256": "abcd1234",
    }))
    assert out["ok"] is True
    peer = reg.get("lan")
    assert peer.tls_verify is False
    assert peer.tls_pinned_sha256 == "abcd1234"


@pytest.mark.asyncio
async def test_add_peer_defaults_tls_verify_true(reg) -> None:
    async def fake_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "schemaVersion": "0.3", "name": "r", "description": "",
            "url": "https://r:8443", "version": "1.0", "skills": [],
        })
    transport = httpx.MockTransport(fake_handler)

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=lambda: transport)
    by_name = {s.name: s for s in specs}
    await by_name["comm.add_peer"].handler({
        "peer_id": "pub", "url": "https://r:8443", "hmac_secret_value": "s",
    })
    peer = reg.get("pub")
    assert peer.tls_verify is True
    assert peer.tls_pinned_sha256 is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_comm_agent/test_mcp_tools.py -k "self_signed or defaults_tls" -v`
Expected: `test_add_peer_self_signed_pins_fingerprint` FAIL —— 当前 `add_peer` 硬编码 `tls_verify=True`,断言 `peer.tls_verify is False` 失败。

- [ ] **Step 3: Read the TLS values from args**

在 `agents/comm_agent/mcp_tools.py` 的 `add_peer` 里,把构造 `Peer` 的那段:

```python
        env_name = _env_var_name_for(peer_id)
        os.environ[env_name] = secret_value
        peer = Peer(
            peer_id=peer_id,
            display_name=display_name,
            url=url,
            hmac_secret_ref=env_name,
            tls_verify=True,
            tls_pinned_sha256=None,
            added_at=_now_iso(),
            last_seen=None,
        )
```

改为:

```python
        env_name = _env_var_name_for(peer_id)
        os.environ[env_name] = secret_value
        peer = Peer(
            peer_id=peer_id,
            display_name=display_name,
            url=url,
            hmac_secret_ref=env_name,
            tls_verify=args.get("tls_verify", True),
            tls_pinned_sha256=args.get("tls_pinned_sha256"),
            added_at=_now_iso(),
            last_seen=None,
        )
```

同时把 `comm.add_peer` 的 `input_schema` 的 `properties` 补上两项(在 `display_name` 后):

```python
                    "display_name": {"type": "string"},
                    "tls_verify": {"type": "boolean"},
                    "tls_pinned_sha256": {"type": ["string", "null"]},
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_comm_agent/test_mcp_tools.py -v`
Expected: 全部 PASS(原 7 个 + 新 2 个 = 9）。

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/mcp_tools.py tests/test_comm_agent/test_mcp_tools.py
git commit -m "feat(comm-agent): comm.add_peer accepts tls_verify/tls_pinned_sha256"
```

---

### Task 2: 命令管道 —— 常量、_unwrap、_comm_call、会话状态字段

**Files:**
- Modify: `orchestrator/repl_commands.py`(顶部导入 + 模块常量 + 模块函数 `_unwrap` + `ReplCommandHandler.__init__` + 方法 `_comm_call`)
- Test: `tests/test_orchestrator/test_repl_commands.py`

- [ ] **Step 1: Write the failing test(测 `_unwrap` 归一化)**

追加到 `tests/test_orchestrator/test_repl_commands.py`:

```python
from types import SimpleNamespace

from orchestrator.repl_commands import _unwrap


def _ok_result(text: str):
    """模拟成功路径:MCP SDK 的 CallToolResult 对象。"""
    return SimpleNamespace(content=[SimpleNamespace(text=text)], isError=False)


def _err_result(text: str) -> dict:
    """模拟失败路径:MCPHost.call_tool 返回的 dict。"""
    return {"content": [{"type": "text", "text": text}], "isError": True}


def test_unwrap_success_object():
    is_error, text = _unwrap(_ok_result('{"ok": true}'))
    assert is_error is False
    assert text == '{"ok": true}'


def test_unwrap_failure_dict():
    is_error, text = _unwrap(_err_result("error: comm-agent unavailable"))
    assert is_error is True
    assert "unavailable" in text


def test_unwrap_empty_content():
    is_error, text = _unwrap(SimpleNamespace(content=[], isError=False))
    assert is_error is False
    assert text == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k unwrap -v`
Expected: ImportError —— `cannot import name '_unwrap'`.

- [ ] **Step 3: Add imports, constant, `_unwrap`, state fields, `_comm_call`**

在 `orchestrator/repl_commands.py` 顶部导入区(现有 `from orchestrator.repl_ui import ReplUI` 下)加:

```python
import json
from typing import Any

from orchestrator.picker import can_use_interactive_picker

COMM_AGENT_ID = "comm-agent"


def _unwrap(result: Any) -> tuple[bool, str]:
    """Normalize MCPHost.call_tool's heterogeneous return.

    Success -> CallToolResult object (.content[0].text, .isError).
    Failure -> plain dict {"content": [{"text": ...}], "isError": True}.
    Returns (is_error, first_text).
    """
    if isinstance(result, dict):
        content = result.get("content") or []
        text = ""
        if content:
            first = content[0]
            text = first.get("text", "") if isinstance(first, dict) else getattr(first, "text", "")
        return bool(result.get("isError")), text
    content = getattr(result, "content", None) or []
    text = getattr(content[0], "text", "") if content else ""
    return bool(getattr(result, "isError", False)), text
```

在 `ReplCommandHandler.__init__` 末尾(现有赋值之后)加两个字段:

```python
        self._current_peer: str | None = None
        self._chat_contexts: dict[str, str] = {}
```

在类里加一个方法(放在 `handle` 之后):

```python
    async def _comm_call(self, tool: str, arguments: dict) -> tuple[bool, Any]:
        """Call a comm.* tool. Returns (ok, payload).

        ok=False with payload as an error string when the specialist failed
        or JSON didn't parse; otherwise payload is the parsed dict.
        """
        result = await self.host.call_tool(COMM_AGENT_ID, tool, arguments)
        is_error, text = _unwrap(result)
        if is_error:
            return False, text or "comm-agent unavailable"
        try:
            return True, json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return False, f"unexpected response: {text[:200]}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k unwrap -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py tests/test_orchestrator/test_repl_commands.py
git commit -m "feat(repl): comm command pipeline (_unwrap, _comm_call, state fields)"
```

---

### Task 3: /comm list(含测试辅助 _CommHost)

**Files:**
- Modify: `orchestrator/repl_commands.py`(`handle` 加 `/comm` 分支 + `_cmd_comm` + `_comm_list`)
- Test: `tests/test_orchestrator/test_repl_commands.py`

- [ ] **Step 1: Write the failing test(并定义复用的 fake host)**

追加到 `tests/test_orchestrator/test_repl_commands.py`(`_CommHost`/`_ok_result` 等供后续 Task 复用):

```python
class _CommHost(_Host):
    """_Host + 可编程的 call_tool。responses: tool_name -> json str 或 callable(args)->json str。"""
    def __init__(self, responses):
        self._responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    async def call_tool(self, agent_id, name, arguments):
        self.calls.append((agent_id, name, arguments))
        r = self._responses.get(name)
        if r is None:
            return {"content": [{"text": f"error: {name} unavailable"}], "isError": True}
        if callable(r):
            r = r(arguments)
        return _ok_result(r)


def _comm_handler(tmp_path, responses):
    _, ui, state, buf = _handler(tmp_path)
    handler = ReplCommandHandler(ui=ui, state=state, host=_CommHost(responses), router=_Router())
    return handler, ui, buf


def test_comm_list_marks_current(tmp_path):
    peers = json.dumps({"peers": [
        {"peer_id": "home", "display_name": "Home", "url": "https://h:8443", "last_seen": None},
        {"peer_id": "lan", "display_name": "Lan", "url": "https://l:8443", "last_seen": None},
    ]})
    handler, ui, buf = _comm_handler(tmp_path, {"comm.list_peers": peers})
    handler._current_peer = "lan"
    assert _call(handler, "/comm list") == LoopAction.CONTINUE
    text = buf.getvalue()
    assert "home" in text and "lan" in text
    assert "★" in text  # 当前对端被标记


def test_comm_list_when_unavailable(tmp_path):
    # responses 里没有 comm.list_peers → _CommHost 返回 isError dict
    handler, ui, buf = _comm_handler(tmp_path, {})
    assert _call(handler, "/comm list") == LoopAction.CONTINUE  # 友好降级,不抛
    assert "unavailable" in buf.getvalue()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k comm_list -v`
Expected: FAIL —— `/comm` 未识别,输出走到 "Unknown command",断言 `"★"` 不在输出里。

- [ ] **Step 3: Add `/comm` dispatch + `_cmd_comm` + `_comm_list`**

在 `handle` 的命令链里,`if command == "/gateway":` 分支之后、`render_command_error("Unknown command", ...)` 之前插入:

```python
            if command == "/comm":
                return await self._cmd_comm(line)
```

在类里加:

```python
    async def _cmd_comm(self, line: str) -> LoopAction:
        parts = line.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2].strip() if len(parts) > 2 else ""
        if sub == "list":
            return await self._comm_list()
        if sub == "use":
            return await self._comm_use(arg)
        if sub == "rm":
            return await self._comm_rm(arg)
        if sub == "add":
            return await self._comm_add()
        self.ui.render_command_error("/comm", "用法:/comm add | list | use <name> | rm <name>")
        return LoopAction.CONTINUE

    async def _comm_list(self) -> LoopAction:
        ok, payload = await self._comm_call("comm.list_peers", {})
        if not ok:
            self.ui.render_command_error("comm 不可用", payload)
            return LoopAction.CONTINUE
        rows = [
            [("★ " if p["peer_id"] == self._current_peer else "") + p["peer_id"],
             p.get("display_name", ""), p.get("url", "")]
            for p in payload.get("peers", [])
        ]
        self.ui.render_table(title="远程对端", columns=["peer_id", "名称", "URL"], rows=rows)
        return LoopAction.CONTINUE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k comm_list -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py tests/test_orchestrator/test_repl_commands.py
git commit -m "feat(repl): /comm list with current-peer marker"
```

---

### Task 4: /comm use(校验存在性)

**Files:**
- Modify: `orchestrator/repl_commands.py`(`_comm_use`)
- Test: `tests/test_orchestrator/test_repl_commands.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_comm_use_sets_current(tmp_path):
    peers = json.dumps({"peers": [{"peer_id": "home", "display_name": "H", "url": "https://h:8443", "last_seen": None}]})
    handler, ui, buf = _comm_handler(tmp_path, {"comm.list_peers": peers})
    assert _call(handler, "/comm use home") == LoopAction.CONTINUE
    assert handler._current_peer == "home"


def test_comm_use_unknown_rejected(tmp_path):
    peers = json.dumps({"peers": [{"peer_id": "home", "display_name": "H", "url": "https://h:8443", "last_seen": None}]})
    handler, ui, buf = _comm_handler(tmp_path, {"comm.list_peers": peers})
    assert _call(handler, "/comm use nope") == LoopAction.CONTINUE
    assert handler._current_peer is None
    assert "nope" in buf.getvalue()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k comm_use -v`
Expected: FAIL —— `_comm_use` 不存在(AttributeError)。

- [ ] **Step 3: Add `_comm_use`**

```python
    async def _comm_use(self, name: str) -> LoopAction:
        if not name:
            self.ui.render_command_error("/comm use", "用法:/comm use <peer_id>")
            return LoopAction.CONTINUE
        ok, payload = await self._comm_call("comm.list_peers", {})
        if not ok:
            self.ui.render_command_error("comm 不可用", payload)
            return LoopAction.CONTINUE
        names = {p["peer_id"] for p in payload.get("peers", [])}
        if name not in names:
            self.ui.render_command_error("未知对端", f"{name!r} 未注册;先 /comm add,或看 /comm list")
            return LoopAction.CONTINUE
        self._current_peer = name
        self.ui.render_text(title="当前对端", text=f"已切换到 {name}")
        return LoopAction.CONTINUE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k comm_use -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py tests/test_orchestrator/test_repl_commands.py
git commit -m "feat(repl): /comm use with existence check"
```

---

### Task 5: /comm rm(删当前对端时清状态)

**Files:**
- Modify: `orchestrator/repl_commands.py`(`_comm_rm`)
- Test: `tests/test_orchestrator/test_repl_commands.py`

- [ ] **Step 1: Write the failing test**

```python
def test_comm_rm_clears_current(tmp_path):
    handler, ui, buf = _comm_handler(tmp_path, {
        "comm.remove_peer": json.dumps({"ok": True, "peer_id": "home", "removed": True}),
    })
    handler._current_peer = "home"
    handler._chat_contexts["home"] = "ctx-1"
    assert _call(handler, "/comm rm home") == LoopAction.CONTINUE
    assert handler._current_peer is None
    assert "home" not in handler._chat_contexts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k comm_rm -v`
Expected: FAIL —— `_comm_rm` 不存在。

- [ ] **Step 3: Add `_comm_rm`**

```python
    async def _comm_rm(self, name: str) -> LoopAction:
        if not name:
            self.ui.render_command_error("/comm rm", "用法:/comm rm <peer_id>")
            return LoopAction.CONTINUE
        ok, payload = await self._comm_call("comm.remove_peer", {"peer_id": name})
        if not ok:
            self.ui.render_command_error("comm 不可用", payload)
            return LoopAction.CONTINUE
        if not payload.get("ok"):
            self.ui.render_command_error("删除失败", payload.get("error", "未知错误"))
            return LoopAction.CONTINUE
        if self._current_peer == name:
            self._current_peer = None
            self._chat_contexts.pop(name, None)
        removed = payload.get("removed", False)
        self.ui.render_text(title="删除对端", text=f"{name}: {'已删除' if removed else '不存在'}")
        return LoopAction.CONTINUE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k comm_rm -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py tests/test_orchestrator/test_repl_commands.py
git commit -m "feat(repl): /comm rm clears current peer + chat context"
```

---

### Task 6: /comm add(交互层 + 可测执行层)

**Files:**
- Modify: `orchestrator/repl_commands.py`(`_comm_add` 交互层 + `_comm_add_execute` 执行层)
- Test: `tests/test_orchestrator/test_repl_commands.py`

- [ ] **Step 1: Write the failing tests(只测执行层)**

```python
def test_comm_add_execute_success_sets_current(tmp_path):
    handler, ui, buf = _comm_handler(tmp_path, {
        "comm.add_peer": json.dumps({
            "ok": True, "peer_id": "home", "env_var_name": "COMM_PEER_HOME_HMAC",
            "fetched_card": None, "note": "export COMM_PEER_HOME_HMAC=<value>",
        }),
    })
    result = asyncio.run(handler._comm_add_execute(
        peer_id="home", url="https://h:8443", display_name="Home",
        self_signed=False, pinned="", secret="s",
    ))
    assert result == LoopAction.CONTINUE
    assert handler._current_peer == "home"
    assert "export COMM_PEER_HOME_HMAC" in buf.getvalue()


def test_comm_add_execute_self_signed_passes_pin(tmp_path):
    handler, ui, buf = _comm_handler(tmp_path, {
        "comm.add_peer": json.dumps({"ok": True, "peer_id": "lan", "note": ""}),
    })
    asyncio.run(handler._comm_add_execute(
        peer_id="lan", url="https://l:8443", display_name="",
        self_signed=True, pinned="abcd1234", secret="s",
    ))
    _, _, args = handler.host.calls[-1]
    assert args["tls_verify"] is False
    assert args["tls_pinned_sha256"] == "abcd1234"


def test_comm_add_execute_failure_keeps_no_current(tmp_path):
    handler, ui, buf = _comm_handler(tmp_path, {
        "comm.add_peer": json.dumps({"ok": False, "error": "tls_verify=False requires pin"}),
    })
    asyncio.run(handler._comm_add_execute(
        peer_id="bad", url="https://b:8443", display_name="",
        self_signed=False, pinned="", secret="s",
    ))
    assert handler._current_peer is None
    assert "tls_verify" in buf.getvalue()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k comm_add -v`
Expected: FAIL —— `_comm_add_execute` 不存在。

- [ ] **Step 3: Add `_comm_add` (interactive) + `_comm_add_execute` (testable)**

```python
    async def _comm_add(self) -> LoopAction:
        if not can_use_interactive_picker():
            self.ui.render_command_error("/comm add 需要 TTY", "请在交互式终端运行")
            return LoopAction.CONTINUE
        from rich.prompt import Prompt
        c = self.ui.console
        peer_id = Prompt.ask("peer_id", console=c).strip()
        url = Prompt.ask("url (https://host:8443)", console=c).strip()
        display_name = Prompt.ask("显示名 (可空)", console=c, default="").strip()
        self_signed = Prompt.ask("自签证书?", console=c, choices=["y", "n"], default="n") == "y"
        pinned = ""
        if self_signed:
            pinned = Prompt.ask("证书 SHA-256 指纹 (hex)", console=c).strip()
        secret = Prompt.ask("HMAC 密钥", console=c, password=True).strip()
        return await self._comm_add_execute(
            peer_id=peer_id, url=url, display_name=display_name,
            self_signed=self_signed, pinned=pinned, secret=secret,
        )

    async def _comm_add_execute(
        self, *, peer_id: str, url: str, display_name: str,
        self_signed: bool, pinned: str, secret: str,
    ) -> LoopAction:
        if not peer_id or not url or not secret:
            self.ui.render_command_error("缺少必填项", "peer_id / url / 密钥 都必填")
            return LoopAction.CONTINUE
        args: dict = {"peer_id": peer_id, "url": url, "hmac_secret_value": secret}
        if display_name:
            args["display_name"] = display_name
        if self_signed:
            args["tls_verify"] = False
            args["tls_pinned_sha256"] = pinned
        ok, payload = await self._comm_call("comm.add_peer", args)
        if not ok:
            self.ui.render_command_error("comm 不可用", payload)
            return LoopAction.CONTINUE
        if not payload.get("ok"):
            self.ui.render_command_error("注册失败", payload.get("error", "未知错误"))
            return LoopAction.CONTINUE
        self._current_peer = peer_id
        note = payload.get("note", "")
        self.ui.render_text(
            title="已注册并设为当前对端",
            text=f"{peer_id}\n持久化密钥(否则重启失效):{note}",
        )
        return LoopAction.CONTINUE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k comm_add -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py tests/test_orchestrator/test_repl_commands.py
git commit -m "feat(repl): /comm add interactive register (self-signed + persist note)"
```

---

### Task 7: /task(委派给当前对端)

**Files:**
- Modify: `orchestrator/repl_commands.py`(`handle` 加 `/task` 分支 + `_require_current_peer` + `_cmd_task`)
- Test: `tests/test_orchestrator/test_repl_commands.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_task_without_current_peer_prompts(tmp_path):
    handler, ui, buf = _comm_handler(tmp_path, {})
    assert _call(handler, "/task do something") == LoopAction.CONTINUE
    assert "当前对端" in buf.getvalue() or "use" in buf.getvalue()
    assert handler.host.calls == []  # 没有当前对端,不应发起调用


def test_task_delegates_to_current_peer(tmp_path):
    handler, ui, buf = _comm_handler(tmp_path, {
        "comm.delegate": json.dumps({
            "ok": True, "events_count": 2, "final_result": "42", "duration_ms": 12,
        }),
    })
    handler._current_peer = "home"
    assert _call(handler, "/task add 1 and 41") == LoopAction.CONTINUE
    _, name, args = handler.host.calls[-1]
    assert name == "comm.delegate"
    assert args["peer_id"] == "home"
    assert args["task"] == "add 1 and 41"
    assert args["stream"] is False
    text = buf.getvalue()
    assert "home" in text  # 回显目标
    assert "42" in text     # 最终结果
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k "task_" -v`
Expected: FAIL —— `/task` 未识别。

- [ ] **Step 3: Add `/task` dispatch + helpers**

在 `handle` 里 `/comm` 分支之后加:

```python
            if command == "/task":
                return await self._cmd_task(line)
```

在类里加:

```python
    def _require_current_peer(self) -> str | None:
        if self._current_peer is None:
            self.ui.render_command_error(
                "没有当前对端", "先 /comm add 注册,或 /comm use <name> 选一个",
            )
        return self._current_peer

    async def _cmd_task(self, line: str) -> LoopAction:
        parts = line.split(maxsplit=1)
        task = parts[1].strip() if len(parts) > 1 else ""
        if not task:
            self.ui.render_command_error("/task", "用法:/task <要委派的任务>")
            return LoopAction.CONTINUE
        peer = self._require_current_peer()
        if peer is None:
            return LoopAction.CONTINUE
        self.ui.render_text(title=f"→ 委派给 {peer}", text=task, style="cyan")
        ok, payload = await self._comm_call(
            "comm.delegate", {"peer_id": peer, "task": task, "stream": False},
        )
        if not ok:
            self.ui.render_command_error("comm 不可用", payload)
            return LoopAction.CONTINUE
        if not payload.get("ok"):
            self.ui.render_command_error("委派失败", payload.get("error", "未知错误"))
            return LoopAction.CONTINUE
        self.ui.render_text(
            title=f"结果 (来自 {peer}, {payload.get('duration_ms', '?')}ms)",
            text=str(payload.get("final_result") or "(无最终结果)"),
        )
        return LoopAction.CONTINUE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k "task_" -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py tests/test_orchestrator/test_repl_commands.py
git commit -m "feat(repl): /task delegate to current peer"
```

---

### Task 8: /chat(对话 + context_id 续传)

**Files:**
- Modify: `orchestrator/repl_commands.py`(`handle` 加 `/chat` 分支 + `_cmd_chat`)
- Test: `tests/test_orchestrator/test_repl_commands.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_chat_first_turn_no_context(tmp_path):
    handler, ui, buf = _comm_handler(tmp_path, {
        "comm.chat": json.dumps({"ok": True, "reply": "hi back", "context_id": "ctx-1"}),
    })
    handler._current_peer = "home"
    assert _call(handler, "/chat hello") == LoopAction.CONTINUE
    _, name, args = handler.host.calls[-1]
    assert name == "comm.chat"
    assert args["peer_id"] == "home"
    assert args["message"] == "hello"
    assert "context_id" not in args  # 首轮不带
    assert handler._chat_contexts["home"] == "ctx-1"  # 记下返回的 context
    assert "hi back" in buf.getvalue()


def test_chat_second_turn_sends_context(tmp_path):
    handler, ui, buf = _comm_handler(tmp_path, {
        "comm.chat": json.dumps({"ok": True, "reply": "ok", "context_id": "ctx-1"}),
    })
    handler._current_peer = "home"
    handler._chat_contexts["home"] = "ctx-1"
    _call(handler, "/chat second turn")
    _, _, args = handler.host.calls[-1]
    assert args["context_id"] == "ctx-1"  # 续传上次 context
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k "chat_" -v`
Expected: FAIL —— `/chat` 未识别。

- [ ] **Step 3: Add `/chat` dispatch + `_cmd_chat`**

在 `handle` 里 `/task` 分支之后加:

```python
            if command == "/chat":
                return await self._cmd_chat(line)
```

在类里加:

```python
    async def _cmd_chat(self, line: str) -> LoopAction:
        parts = line.split(maxsplit=1)
        message = parts[1].strip() if len(parts) > 1 else ""
        if not message:
            self.ui.render_command_error("/chat", "用法:/chat <要说的话>")
            return LoopAction.CONTINUE
        peer = self._require_current_peer()
        if peer is None:
            return LoopAction.CONTINUE
        self.ui.render_text(title=f"→ 对话 {peer}", text=message, style="cyan")
        args: dict = {"peer_id": peer, "message": message}
        ctx = self._chat_contexts.get(peer)
        if ctx:
            args["context_id"] = ctx
        ok, payload = await self._comm_call("comm.chat", args)
        if not ok:
            self.ui.render_command_error("comm 不可用", payload)
            return LoopAction.CONTINUE
        if not payload.get("ok"):
            self.ui.render_command_error("对话失败", payload.get("error", "未知错误"))
            return LoopAction.CONTINUE
        new_ctx = payload.get("context_id")
        if new_ctx:
            self._chat_contexts[peer] = new_ctx
        self.ui.render_text(title=f"{peer} 回复", text=str(payload.get("reply", "")))
        return LoopAction.CONTINUE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k "chat_" -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py tests/test_orchestrator/test_repl_commands.py
git commit -m "feat(repl): /chat with per-peer context_id continuity"
```

---

### Task 9: /help 帮助文本 + 全量回归

**Files:**
- Modify: `orchestrator/repl_ui.py`(`COMMANDS` dict)
- Test: `tests/test_orchestrator/test_repl_commands.py`

- [ ] **Step 1: Write the failing test**

```python
def test_help_lists_comm_commands(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)
    _call(handler, "/help")
    text = buf.getvalue()
    assert "/comm" in text
    assert "/task" in text
    assert "/chat" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k help_lists_comm -v`
Expected: FAIL —— `COMMANDS` 里还没有这三条。

- [ ] **Step 3: Add entries to `COMMANDS`**

在 `orchestrator/repl_ui.py` 的 `COMMANDS` dict 里加三条(放在已有条目之间合适处):

```python
    "/comm": "管理远程 A2A 对端:/comm add | list | use <name> | rm <name>",
    "/task": "把一句话作为任务委派给当前对端(/task <任务>)",
    "/chat": "把一句话作为对话发给当前对端(/chat <消息>)",
```

- [ ] **Step 4: Run test + full regression**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py -k help_lists_comm -v`
Expected: PASS.

Run: `python -m pytest tests/test_orchestrator/ tests/test_comm_agent/ -q`
Expected: 全绿(无回归)。

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_ui.py tests/test_orchestrator/test_repl_commands.py
git commit -m "feat(repl): document /comm /task /chat in help"
```

---

## 完成

实现后:`/comm add|list|use|rm` + `/task` + `/chat` 可在 REPL 直接驱动 comm-agent,绕过 planner。运行 `python -m pytest -q` 跑全套件确认无回归,再用 finishing-a-development-branch 收尾。
