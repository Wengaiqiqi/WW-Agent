# Gateway Action Menu — 实时日志尾部面板

## 背景

`/gateway` 进入平台后，进入 action 菜单（`QQ Official Bot -- choose action` /
`Feishu / Lark -- choose action`），用户看到几行 action 选项后下方是大片
空白。后台 gateway 任务（QQ / Feishu）实际上正在写日志到
`<config_dir>/gateway.log`，但用户在 REPL 中无法直接看到日志，必须开另一个
终端 `tail -f` 才能观察连接状态、事件分发等运行情况。

## 目标

在 action 菜单下方加一个**实时刷新**的日志面板，显示当前平台的最后 8 行
日志。要求：

- 仅作用于 action 菜单（顶层平台选择菜单不变）。
- 每个平台只显示与自己相关的日志（QQ 看 QQ，飞书看飞书 + lark + uvicorn）。
- 不破坏现有 picker 的其它调用方（`/model` 等）。

## 非目标（YAGNI）

- 不按日志级别上色。
- 不做日志面板内的滚动 / 搜索（要看完整历史 → 直接看 `gateway.log` 文件）。
- 不在顶层平台列表加日志面板。
- 不动 `_install_file_logging`、不改日志格式、不动 gateway 适配器本身。

## 架构

```
orchestrator/repl_commands.py::_gw_platform_menu
        │
        ├── 构造 footer_lines 闭包  ──────────►  gateway.log_tail.read_tail()
        │                                            │
        │                                            └── 读取 <config_dir>/gateway.log
        │                                                按平台过滤 + 截断 + 返回 list[str]
        │
        └── 调 interactive_select_async(..., footer_lines=..., footer_refresh_seconds=0.2)
                │
                └── orchestrator/picker.py
                    在 HSplit 末尾追加 footer 标题 + 内容两个 Window
                    若 footer_refresh_seconds 非空，注册周期性 app.invalidate()
                    每次 invalidate 触发 render → 重新调 footer_lines() → 重新读文件
```

数据流：单向、每次刷新无副作用、不缓存。文件 IO 走同步 `open().readlines()`
就够 —— 日志文件一般 < 1 MB，读尾部 8 行的开销可忽略；prompt_toolkit 的
渲染线程不会阻塞 REPL 主循环（picker 本身就跑在 worker thread 里）。

## 组件

### 1. `gateway/log_tail.py`（新增）

公开一个函数：

```python
def read_tail(
    path: Path,
    *,
    platform: str,
    max_lines: int = 8,
    max_width: int | None = None,
) -> list[str]:
    """读 gateway.log 末尾，过滤到指定平台，返回 ≤ max_lines 行。

    - path 不存在 → 返回 []
    - 任何 IO / 解码异常 → 返回 []（用 errors="replace" 避免抛）
    - max_width 给定时，每行右侧硬截断（保留 "…" 结尾以提示截断）
    """
```

平台过滤规则：

| platform | 命中条件（任一即可） |
|----------|----------------------|
| `qq`     | 行内出现 `gateway[qq]`；或日志 logger 段（`%(name)s` 字段）以 `gateway.qq` 开头；或字段值精确等于 `qq` |
| `feishu` | 行内出现 `gateway[feishu]`；或 logger 段以 `gateway.feishu` / `lark_oapi` / `uvicorn` 开头；或字段值精确等于 `feishu` |

`_install_file_logging` 用的格式是
`%(asctime)s %(levelname)-7s %(name)s | %(message)s`，logger 名在第三列。
解析时直接按前缀字符串匹配即可，不需要正则。

实现思路（避免读整个文件）：

- 文件较小（典型 < 1 MB）时直接 `path.read_text(errors="replace").splitlines()`
  然后从末尾倒着扫，收集 max_lines 条命中的行。
- 大文件优化留到日志真正变大再做（当前 `_install_file_logging` 没设
  rotation，文件无限增长是另一个问题，不在本 spec 范围）。

返回顺序：**时间正序**（旧 → 新），调用方直接拼成多行字符串即可。

### 2. `orchestrator/picker.py`（修改）

`interactive_select` 与 `interactive_select_async` 各加三个可选参数：

```python
footer_lines: Callable[[], list[str]] | None = None
footer_title: str | None = None
footer_refresh_seconds: float | None = None
```

`footer_lines` 为 `None` 时一切不变（向后兼容）。

非 `None` 时，在原有 HSplit 末尾追加：

1. 一个 `Window`（高度 1）显示 `footer_title`，灰色样式。
2. 一个 `Window`（高度 `max_lines` 即调用方约定的 8）显示
   `footer_lines()` 拼接后的内容，灰色样式。
3. 若 `footer_refresh_seconds` 非空，picker 启动后用
   `app.create_background_task` + `asyncio.sleep` 循环周期性
   `app.invalidate()`；picker 退出时该任务自然随 app 终止。

placeholder：当 `footer_lines()` 返回空列表时，显示一行
`(no log yet — start the gateway to see activity)`，避免面板"塌陷"。

样式：复用现有 `class:dim`（灰色），不引入新颜色。

### 3. `orchestrator/repl_commands._gw_platform_menu`（修改）

在调用 `interactive_select_async` 时新增三个参数：

```python
from agent_paths import config_dir
from gateway.log_tail import read_tail

log_path = config_dir() / "gateway.log"

def _footer() -> list[str]:
    return read_tail(
        log_path,
        platform=platform,
        max_lines=8,
        max_width=self.ui.console.width - 4,
    )

idx = await interactive_select_async(
    f"{label} -- choose action",
    rows,
    default_index=0,
    instruction="up/down move - enter run - esc back",
    footer_lines=_footer,
    footer_title="Recent log (last 8 lines, filtered)",
    footer_refresh_seconds=0.2,
)
```

`config_dir()` 已在 `agent_paths.py` 暴露（见 `gateway/manager.py:43`
`from agent_paths import config_dir` 同一来源）。

## 测试计划

只覆盖 `gateway.log_tail.read_tail` —— 这是唯一有逻辑的纯函数；
picker 和 repl 改动是 UI 装配，跑通即可，无单元测试价值。

`tests/test_gateway_log_tail.py` 用例：

| 用例 | 输入 | 期望 |
|------|------|------|
| `test_file_missing_returns_empty` | path 指向不存在的文件 | `[]` |
| `test_qq_filter_matches_bracket_marker` | 行含 `gateway[qq]` | 命中 |
| `test_qq_filter_matches_logger_name` | logger 段为 `gateway.qq` | 命中 |
| `test_qq_filter_rejects_feishu` | 行属于 feishu | 不命中 |
| `test_feishu_filter_matches_lark_oapi` | logger 段以 `lark_oapi` 开头 | 命中 |
| `test_feishu_filter_matches_uvicorn` | logger 段以 `uvicorn.access` 开头 | 命中 |
| `test_max_lines_caps_result` | 给 20 行命中、`max_lines=8` | 返回最后 8 行，时间正序 |
| `test_max_width_truncates_with_ellipsis` | 行宽超过 `max_width` | 末尾以 `…` 截断 |
| `test_unicode_decode_replace` | 文件含非法 UTF-8 字节 | 不抛，正常返回（被替换为 �） |

UI 手测（不写自动化）：

- 启动 REPL → `/gateway` → 选 QQ → 看到下方面板，显示
  "(no log yet — ...)"。
- "Start gateway" → 面板开始填充 QQ 行；切到飞书 menu，看不到 QQ 行。
- 同时启动飞书 → 飞书 menu 显示 lark_oapi / gateway[feishu] 行，QQ menu
  不混入 lark 行。
- 关闭 gateway → 面板停止增长，最后 8 行仍在。
- 终端窗口缩窄 → 行被截断，不换行破坏布局。

## 错误处理

| 情形 | 行为 |
|------|------|
| `gateway.log` 不存在 | `read_tail` 返回 `[]` → 面板显示占位提示 |
| 文件读取权限错误 | 同上，吞掉异常返回 `[]`（picker UI 不能崩） |
| 文件包含非 UTF-8 字节 | `errors="replace"` 自动替换 |
| 行内含 ANSI 转义码 | 直接原样输出（日志文件目前不带颜色） |
| 终端 resize 期间面板高度对不上 | prompt_toolkit 自己处理 invalidate，下一次 0.2s tick 刷新即可 |

## 兼容性

- `interactive_select` 的三个新参数都是 keyword-only 且默认 `None`，
  现有调用点（`_gw_pick_platform`、`_gw_pick_feishu_mode`、legacy 的
  `/model` 等）零改动。
- 不影响 `python -m gateway feishu` / `python -m gateway qq` 的独立运行
  路径 —— 那条路径根本不进 picker。
- 不动 `_install_file_logging`，日志文件路径和格式不变。

## 文件清单

- 新增：`gateway/log_tail.py`
- 新增：`tests/test_gateway_log_tail.py`
- 修改：`orchestrator/picker.py`（加 footer 支持）
- 修改：`orchestrator/repl_commands.py`（`_gw_platform_menu` 接入 footer）
