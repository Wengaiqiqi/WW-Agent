# 拆分 repl_commands.py（组合委托）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 1368 行的巨型类 `ReplCommandHandler` 按命令域拆成 4 个独立子 handler（组合委托），主类退化成"装配 + 路由"，纯结构重构、零行为变化。

**Architecture:** 方案 B（组合委托）。新建 `core_commands.py` / `model_wizard.py` / `gateway_commands.py` / `remote_commands.py`，每个类构造时注入自己需要的依赖（`ui`/`state`/`host`/`router`）。`ReplCommandHandler` 持有这 4 个实例，`handle()` 用命令名→协程的字典路由分发。状态字段 `_current_peer`/`_chat_contexts` 迁入 `RemoteCommands`，主类用 property 代理保持现有测试可用。

**Tech Stack:** Python 3.11+ / pytest / asyncio。

设计来源：本会话对话（已获用户批准 B 方案）。

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `orchestrator/repl_commands.py` | 瘦路由器：装配 4 个子 handler + 命令字典分发 + 异常兜底 + property 代理 | Rewrite（缩到 ~140 行）|
| `orchestrator/repl_core_commands.py` | `CoreCommands`：`/help /agents /tools /permissions /config /status /skills /instructions /clear /compact` | Create |
| `orchestrator/repl_model_wizard.py` | `ModelWizard`：`/model` + 全部 `_mw_*` 交互步骤 | Create |
| `orchestrator/repl_gateway_commands.py` | `GatewayCommands`：`/gateway` + 全部 `_gw_*` / `_ask_field` / `_coerce_field` / `_parse_concurrency` | Create |
| `orchestrator/repl_remote_commands.py` | `RemoteCommands`：`/comm /task /chat` + `_comm_*` + peer 持久化 + `_current_peer`/`_chat_contexts` | Create |
| `tests/test_orchestrator/test_repl_commands.py` | 适配：`_cmd_help` monkeypatch → `_core.help`；`_parse_concurrency` 类引用 → `GatewayCommands` | Modify |
| `tests/test_orchestrator/test_repl_comm.py` | 适配：`_comm_add_execute` 调用 → `handler._remote._comm_add_execute` | Modify |

**依赖注入约定**（每个子 handler 的 `__init__`）：
- `CoreCommands(*, ui, state, host, router)` — `/agents` 用 host/router，`/tools` 用 router，`/permissions`/`/config`/`/status`/`/skills`/`/instructions` 用 state。
- `ModelWizard(*, ui, state)` — 模型配置写 state/凭据，无 host/router 依赖。
- `GatewayCommands(*, ui)` — 网关读写 `gateway.*` 自己的配置/manager，仅需 ui。
- `RemoteCommands(*, ui, host)` — comm/task/chat 走 `host.call_tool`；自管 `_current_peer`/`_chat_contexts`。

**子 handler 公共入口命名**（主类路由表用）：
- `CoreCommands`: `help(line)`, `agents(line)`, `tools(line)`, `permissions(line)`, `config(line)`, `status(line)`, `skills(line)`, `instructions(line)`, `clear(line)`, `compact(line)` —— 全部接收 `line: str` 参数（即使不用，统一签名便于路由表）。
- `ModelWizard`: `async run(line)`。
- `GatewayCommands`: `async run(line)`。
- `RemoteCommands`: `async comm(line)`, `async task(line)`, `async chat(line)`。

> **迁移手法**：方法体**原样搬迁**（剪切粘贴，不改逻辑），仅做三类机械改动：(1) 把对外入口方法改成上面的公共名（去掉 `_cmd_` 前缀并统一 `line` 签名）；(2) 跨方法调用 `self._cmd_x()` → 同类内仍 `self.x()`；(3) 模块级符号（`COMM_AGENT_ID`、`_unwrap`、`_load_persisted_peer`、`_persist_peer`）跟着搬到使用它的文件。

执行前置：工作目录为仓库根，当前分支 `refactor/split-repl-commands`。每个 Task 完成后跑该域的测试 + 提交。

---

### Task 0: 基线快照

- [ ] **Step 1: 记录当前测试基线全绿**

Run: `python -m pytest tests/test_orchestrator/ -q`
Expected: all passed（记下数字，作为每步回归基准）。

---

### Task 1: 抽出 `ModelWizard`（最独立，无持久状态）

**Files:**
- Create: `orchestrator/repl_model_wizard.py`
- Modify: `orchestrator/repl_commands.py`（删除已搬走的方法，`/model` 改为委托）

- [ ] **Step 1: 创建 `repl_model_wizard.py`，搬入 `_cmd_model` + 所有 `_mw_*`**

把 `repl_commands.py` 当前第 208–467 行的这 7 个方法**原样剪切**到新文件：`_cmd_model`（改名为 `run`）、`_mw_print_intro`、`_mw_select_provider`、`_mw_select_model`、`_mw_enter_api_key`、`_mw_enter_base_url`。新文件骨架：

```python
from __future__ import annotations

from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class ModelWizard:
    def __init__(self, *, ui: ReplUI, state):
        self.ui = ui
        self.state = state

    async def run(self, line: str) -> LoopAction:
        # —— 原 _cmd_model 的方法体原样粘贴在此（其内部对 self._mw_* 的调用保持不变）——
        ...

    # —— 原 _mw_print_intro / _mw_select_provider / _mw_select_model /
    #    _mw_enter_api_key / _mw_enter_base_url 原样粘贴，方法名不变 ——
```

迁移时确认：`_cmd_model` 原方法体内若有 `from ... import ...` 局部导入，一并带过来；它引用的 `self.ui` / `self.state` 在新类构造里已具备。

- [ ] **Step 2: 在 `repl_commands.py` 删除这 7 个方法，`/model` 改为委托**

在 `ReplCommandHandler.__init__` 末尾加：
```python
        from orchestrator.repl_model_wizard import ModelWizard
        self._model = ModelWizard(ui=ui, state=state)
```
把 `handle()` 里的：
```python
            if command == "/model":
                return await self._cmd_model(line)
```
改为：
```python
            if command == "/model":
                return await self._model.run(line)
```
并删除原 `_cmd_model` 及 5 个 `_mw_*` 方法定义。

- [ ] **Step 3: 跑模型相关测试 + 全 orchestrator 套件**

Run: `python -m pytest tests/test_orchestrator/ -q`
Expected: 与 Task 0 同样全绿（`/model` 走交互需 TTY，单测覆盖的是非交互路径；不应有新增失败）。

- [ ] **Step 4: Commit**

```bash
git add orchestrator/repl_commands.py orchestrator/repl_model_wizard.py
git commit -m "refactor(repl): extract ModelWizard from ReplCommandHandler"
```

---

### Task 2: 抽出 `GatewayCommands`（体量最大）

**Files:**
- Create: `orchestrator/repl_gateway_commands.py`
- Modify: `orchestrator/repl_commands.py`
- Modify: `tests/test_orchestrator/test_repl_commands.py`（`_parse_concurrency` 类引用）

- [ ] **Step 1: 创建 `repl_gateway_commands.py`，搬入 `_cmd_gateway` + 所有 `_gw_*` 等**

把当前第 526–1022 行的全部网关方法**原样剪切**到新文件：`_cmd_gateway`（改名 `run`）、`_gw_pick_platform`、`_gw_platform_menu`、`_gw_platform_row`、`_gw_print_overview`、`_gw_setup`、`_gw_pick_feishu_mode`、`_parse_concurrency`、`_gw_start`、`_gw_stop`、`_gw_view`、`_gw_clear`、`_gw_fields`、`_gw_field_specs`、`_coerce_field`、`_gw_display`、`_ask_field`。骨架：

```python
from __future__ import annotations

from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class GatewayCommands:
    def __init__(self, *, ui: ReplUI):
        self.ui = ui

    async def run(self, line: str) -> LoopAction:
        # —— 原 _cmd_gateway 方法体原样粘贴（内部 self._gw_* 调用不变）——
        ...

    # —— 其余 _gw_* / _ask_field / _coerce_field / _gw_fields / _gw_field_specs /
    #    _gw_display / _parse_concurrency 原样粘贴，名字不变 ——
    #    注意：_parse_concurrency / _gw_fields / _gw_field_specs / _coerce_field
    #    若原本是 @staticmethod，保持 @staticmethod 不变。
```

迁移时确认：网关方法内对 `gateway.*` 模块的局部 `import`（如 `from gateway.manager import ...`）原样带过来。

- [ ] **Step 2: 在 `repl_commands.py` 删除网关方法，`/gateway` 改为委托**

`__init__` 加：
```python
        from orchestrator.repl_gateway_commands import GatewayCommands
        self._gateway = GatewayCommands(ui=ui)
```
`handle()` 里 `/gateway` 改为：
```python
            if command == "/gateway":
                return await self._gateway.run(line)
```
删除原 `_cmd_gateway` 及全部 `_gw_*` / `_ask_field` / `_coerce_field` / `_parse_concurrency` 定义。

- [ ] **Step 3: 适配 `test_repl_commands.py::test_parse_concurrency` 的类引用**

`tests/test_orchestrator/test_repl_commands.py` 第 212 行，把：
```python
    from orchestrator.repl_commands import ReplCommandHandler as H
```
改为：
```python
    from orchestrator.repl_gateway_commands import GatewayCommands as H
```
（下面 `H._parse_concurrency(...)` 调用不变。）

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tests/test_orchestrator/test_repl_commands.py tests/test_orchestrator/test_gateway_fields.py -q`
Expected: 全绿（`test_gateway_fields.py` 覆盖 `_gw_field_specs`/`_coerce_field` 等，验证搬迁无误）。

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py orchestrator/repl_gateway_commands.py tests/test_orchestrator/test_repl_commands.py
git commit -m "refactor(repl): extract GatewayCommands from ReplCommandHandler"
```

---

### Task 3: 抽出 `RemoteCommands`（含状态 + peer 持久化）

**Files:**
- Create: `orchestrator/repl_remote_commands.py`
- Modify: `orchestrator/repl_commands.py`（删方法、加委托、加 property 代理）
- Modify: `tests/test_orchestrator/test_repl_comm.py`（`_comm_add_execute` 调用指向 `_remote`）

- [ ] **Step 1: 创建 `repl_remote_commands.py`，搬入远程协作整块**

把模块级 `_load_persisted_peer`(13–28)、`_persist_peer`(29–46)、常量 `COMM_AGENT_ID`(10)，以及第 1051–1368 行的方法（`_comm_call`、`_require_current_peer`、`_cmd_comm`、`_comm_list`、`_comm_use`、`_comm_rm`、`_comm_add`、`_comm_add_execute`、`_cmd_task`、`_cmd_chat`）**原样剪切**到新文件。骨架：

```python
from __future__ import annotations

import json
from typing import Any

from orchestrator.mcp_host import unwrap_tool_result as _unwrap
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI

COMM_AGENT_ID = "comm-agent"


def _load_persisted_peer() -> str | None:
    ...   # 原样

def _persist_peer(peer_id: str | None) -> None:
    ...   # 原样


class RemoteCommands:
    def __init__(self, *, ui: ReplUI, host):
        self.ui = ui
        self.host = host
        self._current_peer: str | None = _load_persisted_peer()
        self._chat_contexts: dict[str, str] = {}

    async def comm(self, line: str) -> LoopAction:
        ...   # 原 _cmd_comm 方法体原样（内部 self._comm_* 调用不变）

    async def task(self, line: str) -> LoopAction:
        ...   # 原 _cmd_task

    async def chat(self, line: str) -> LoopAction:
        ...   # 原 _cmd_chat

    # —— _comm_call / _require_current_peer / _comm_list / _comm_use /
    #    _comm_rm / _comm_add / _comm_add_execute 原样粘贴，名字不变 ——
```

迁移确认：原 `_cmd_comm`/`task`/`chat` 内对 `self.host.call_tool(COMM_AGENT_ID, ...)`、`_unwrap(...)`、`_persist_peer(...)` 的引用，在新文件里都已具备（同文件常量/函数 + 构造注入的 host）。

- [ ] **Step 2: 在 `repl_commands.py` 删除远程块，加委托 + property 代理**

`__init__` 里删除 `self._current_peer = _load_persisted_peer()` 与 `self._chat_contexts = {}` 两行，改为：
```python
        from orchestrator.repl_remote_commands import RemoteCommands
        self._remote = RemoteCommands(ui=ui, host=host)
```
`handle()` 里三条改为委托：
```python
            if command == "/comm":
                return await self._remote.comm(line)
            if command == "/task":
                return await self._remote.task(line)
            if command == "/chat":
                return await self._remote.chat(line)
```
删除原 `_cmd_comm`/`_comm_*`/`_cmd_task`/`_cmd_chat`/`_comm_call`/`_require_current_peer` 定义，以及模块级 `_load_persisted_peer`/`_persist_peer`（已搬走）。在类体加 property 代理（保持现有测试对 `handler._current_peer` / `handler._chat_contexts` 的读写）：
```python
    @property
    def _current_peer(self) -> str | None:
        return self._remote._current_peer

    @_current_peer.setter
    def _current_peer(self, value: str | None) -> None:
        self._remote._current_peer = value

    @property
    def _chat_contexts(self) -> dict[str, str]:
        return self._remote._chat_contexts
```

- [ ] **Step 3: 适配 `test_repl_comm.py` 的 `_comm_add_execute` 直调（2 处）**

`tests/test_orchestrator/test_repl_comm.py`，把两处：
```python
    result = asyncio.run(handler._comm_add_execute(
```
和
```python
    asyncio.run(handler._comm_add_execute(
```
分别改为 `handler._remote._comm_add_execute(`。其余 `handler._current_peer` / `handler._chat_contexts` 由 Step 2 的 property 代理兜住，不改。
（同文件第 12 行 `from orchestrator.repl_commands import ReplCommandHandler, _unwrap` 里的 `_unwrap` 仍可从 repl_commands 导入——见 Task 5 注；本步不动该 import。）

- [ ] **Step 4: 跑远程协作测试**

Run: `python -m pytest tests/test_orchestrator/test_repl_comm.py -q`
Expected: 全绿（property 代理 + `_remote._comm_add_execute` 适配后，24 个用例不应有失败）。

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py orchestrator/repl_remote_commands.py tests/test_orchestrator/test_repl_comm.py
git commit -m "refactor(repl): extract RemoteCommands (comm/task/chat + peer state)"
```

---

### Task 4: 抽出 `CoreCommands` + 路由表收尾

**Files:**
- Create: `orchestrator/repl_core_commands.py`
- Modify: `orchestrator/repl_commands.py`
- Modify: `tests/test_orchestrator/test_repl_commands.py`（`_cmd_help` monkeypatch → `_core.help`）

- [ ] **Step 1: 创建 `repl_core_commands.py`，搬入 9 个瘦命令**

把 `_cmd_help`、`_cmd_agents`、`_cmd_tools`、`_cmd_permissions`、`_cmd_config`、`_cmd_status`、`_cmd_skills`、`_cmd_instructions`、`_cmd_clear`、`_cmd_compact` **原样剪切**到新文件，并把对外名去掉 `_cmd_` 前缀、统一 `line: str` 签名。骨架：

```python
from __future__ import annotations

from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class CoreCommands:
    def __init__(self, *, ui: ReplUI, state, host, router):
        self.ui = ui
        self.state = state
        self.host = host
        self.router = router

    def help(self, line: str) -> LoopAction:
        ...   # 原 _cmd_help 方法体（原本无参；签名加 line 但不用）
    def agents(self, line: str) -> LoopAction:
        ...   # 原 _cmd_agents
    def tools(self, line: str) -> LoopAction:
        ...   # 原 _cmd_tools
    def permissions(self, line: str) -> LoopAction:
        ...   # 原 _cmd_permissions（本就收 line，签名一致）
    def config(self, line: str) -> LoopAction:
        ...   # 原 _cmd_config
    def status(self, line: str) -> LoopAction:
        ...   # 原 _cmd_status
    def skills(self, line: str) -> LoopAction:
        ...   # 原 _cmd_skills
    def instructions(self, line: str) -> LoopAction:
        ...   # 原 _cmd_instructions
    def clear(self, line: str) -> LoopAction:
        ...   # 原 _cmd_clear
    def compact(self, line: str) -> LoopAction:
        ...   # 原 _cmd_compact
```

> 原本无参的方法（如 `_cmd_help(self)`）改成 `help(self, line)`，方法体内不使用 `line`；`_cmd_permissions(self, line)` 已有 `line`，对应 `permissions(self, line)`。

- [ ] **Step 2: 改写 `repl_commands.py` 为瘦路由器（最终形态）**

用以下完整内容替换 `repl_commands.py`（此时 4 个子 handler 都已存在）：

```python
from __future__ import annotations

from typing import Any, Awaitable, Callable

from orchestrator.repl_core_commands import CoreCommands
from orchestrator.repl_gateway_commands import GatewayCommands
from orchestrator.repl_model_wizard import ModelWizard
from orchestrator.repl_remote_commands import RemoteCommands
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class ReplCommandHandler:
    def __init__(self, *, ui: ReplUI, state, host, router):
        self.ui = ui
        self.state = state
        self.host = host
        self.router = router
        self._core = CoreCommands(ui=ui, state=state, host=host, router=router)
        self._model = ModelWizard(ui=ui, state=state)
        self._gateway = GatewayCommands(ui=ui)
        self._remote = RemoteCommands(ui=ui, host=host)
        # command name -> handler returning LoopAction (sync) or Awaitable[LoopAction]
        self._routes: dict[str, Callable[[str], Any]] = {
            "/help": self._core.help,
            "/status": self._core.status,
            "/agents": self._core.agents,
            "/tools": self._core.tools,
            "/permissions": self._core.permissions,
            "/config": self._core.config,
            "/skills": self._core.skills,
            "/instructions": self._core.instructions,
            "/clear": self._core.clear,
            "/compact": self._core.compact,
            "/model": self._model.run,
            "/gateway": self._gateway.run,
            "/comm": self._remote.comm,
            "/task": self._remote.task,
            "/chat": self._remote.chat,
        }

    # State exposed for tests / status rendering; lives on the remote handler.
    @property
    def _current_peer(self) -> str | None:
        return self._remote._current_peer

    @_current_peer.setter
    def _current_peer(self, value: str | None) -> None:
        self._remote._current_peer = value

    @property
    def _chat_contexts(self) -> dict[str, str]:
        return self._remote._chat_contexts

    async def handle(self, line: str) -> LoopAction | None:
        """Returns LoopAction for recognized slash commands, None for non-commands."""
        command = line.split(maxsplit=1)[0].lower()
        if not command.startswith("/"):
            return None
        if command == "/exit":
            return LoopAction.EXIT
        fn = self._routes.get(command)
        if fn is None:
            self.ui.render_command_error(
                "Unknown command",
                f"{command} — type /help for available commands.",
            )
            return LoopAction.CONTINUE
        try:
            result = fn(line)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except Exception as exc:  # noqa: BLE001 - top-level command guard
            self.ui.render_command_error(f"Command error: {command}", str(exc))
            return LoopAction.CONTINUE
```

> `result = fn(line); if hasattr(result, "__await__"): result = await result` 这一段统一处理同步命令（直接返回 `LoopAction`）与异步命令（返回协程）。`/exit` 仍特判在路由前。

- [ ] **Step 3: 适配 `test_repl_commands.py::test_command_exception_is_caught`**

该测试 monkeypatch `handler._cmd_help` 验证异常兜底；`_cmd_help` 已搬入 `CoreCommands.help`，且路由表在 `__init__` 时已绑定 `self._core.help`。把测试改为 patch 子对象、并重建路由绑定，最简做法是直接 patch 路由表项。`tests/test_orchestrator/test_repl_commands.py` 第 194–208 行替换为：

```python
def test_command_exception_is_caught(tmp_path):
    handler, ui, state, buf = _handler(tmp_path)

    def _broken(line):
        raise ValueError("boom")

    handler._routes["/help"] = _broken
    result = _call(handler, "/help")
    assert result == LoopAction.CONTINUE
    assert "boom" in buf.getvalue()
```

（路由表是异常兜底的真实入口，直接替换该项最贴合新结构。）

- [ ] **Step 4: 跑全 orchestrator 套件**

Run: `python -m pytest tests/test_orchestrator/ -q`
Expected: 与 Task 0 数量一致、全绿。

- [ ] **Step 5: Commit**

```bash
git add orchestrator/repl_commands.py orchestrator/repl_core_commands.py tests/test_orchestrator/test_repl_commands.py
git commit -m "refactor(repl): extract CoreCommands; route via dispatch table"
```

---

### Task 5: 全量验证与收尾

- [ ] **Step 1: 确认 `repl_commands.py` 已瘦身**

Run: `wc -l orchestrator/repl_commands.py orchestrator/repl_core_commands.py orchestrator/repl_gateway_commands.py orchestrator/repl_model_wizard.py orchestrator/repl_remote_commands.py`
Expected: `repl_commands.py` ≈ 140 行；其余四个文件合计 ≈ 原 1200 行；无文件 > ~520 行。

- [ ] **Step 2: 确认没有遗留对已删方法的引用**

Run: `grep -rnE '_cmd_model|_cmd_gateway|_cmd_comm|_cmd_task|_cmd_chat|_cmd_help|_gw_|_mw_|_comm_call' orchestrator/repl_commands.py`
Expected: 无输出（除 property/路由表外，旧方法名不应再出现在主文件）。

- [ ] **Step 3: 检查全仓是否有别的模块引用了被搬走的符号**

Run: `grep -rnE 'from orchestrator.repl_commands import|repl_commands\.(_|ReplCommandHandler)' --include='*.py' orchestrator gateway web agents tests`
Expected: 只剩对 `ReplCommandHandler` 与 `_unwrap` 的导入（`test_repl_comm.py:12` 的 `_unwrap` 仍从 `repl_commands` 可见——它通过 `from orchestrator.mcp_host import unwrap_tool_result as _unwrap` 在主文件已不再导入；若该 import 被删，需把 `test_repl_comm.py:12` 改为 `from orchestrator.mcp_host import unwrap_tool_result as _unwrap`）。按实际报错修正。

- [ ] **Step 4: 全量测试套件**

Run: `python -m pytest -q`
Expected: 全绿，与重构前数量一致（880 区间，0 failed）。

- [ ] **Step 5: 手动冒烟（可选但推荐）**

启动 REPL，逐个敲 `/help`、`/status`、`/agents`、`/tools`、`/permissions`、`/config`、`/skills`、`/instructions`、`/clear`、`/compact`、`/comm list`，确认输出与重构前一致；`/model`、`/gateway` 进入交互菜单后用 Esc/q 退出无异常。

- [ ] **Step 6: 最终提交（如 Step 3 有 import 修正）**

```bash
git add -A
git commit -m "refactor(repl): finalize split — verification fixups"
```

---

## 收尾

全部 Task 完成后：
- `ReplCommandHandler` 缩到 ~140 行的路由器；4 个子 handler 各自单一职责、可独立测试。
- `python -m pytest -q` 全绿，行为零变化。
- 旧的 `handler._current_peer` / `handler._chat_contexts` 经 property 代理仍可用，测试改动最小（共约 5 处）。
- 可选：用 `superpowers:finishing-a-development-branch` 决定合并/PR。
