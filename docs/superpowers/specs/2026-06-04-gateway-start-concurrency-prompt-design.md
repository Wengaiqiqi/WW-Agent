# 设计：`/gateway` 启动时选择并发

日期：2026-06-04

## 背景

网关的并发上限目前**只**由环境变量 `GATEWAY_MAX_CONCURRENCY` 控制，且该值在**模块导入时只读一次**，生成两个进程级信号量：

- `gateway/runner.py` 的 `_GATEWAY_SEMAPHORE`（`asyncio.Semaphore`）——约束同一事件循环里的调用方（REPL / QQ 单线程循环）。
- `gateway/feishu_ws.py` 的 `_dispatch_sem`（`threading.BoundedSemaphore`）——Feishu SDK 每条消息一个线程、各自 `asyncio.run`，跨线程必须用线程信号量约束。

因为网关跑在 REPL 同一个进程里的后台任务，单纯改环境变量无法在运行期生效——必须能在启动时**重新配置**这两个信号量。

## 目标

在 REPL 的 `/gateway` 菜单里点 **Start gateway** 时，弹出一个数字输入框，让用户选择并发数：`1` = 串行（关闭并发），`>1` = 并发。

范围与持久化（已与用户确认）：

- **进程级全局单值**：一个并发值作用于整个 REPL/网关进程，贴合现状的共享信号量语义。
- **不持久化**：每次 Start 都询问；默认值取当前生效值（初始来自环境变量）。
- **输入形式**：数字输入框（沿用 Setup 向导的 `_ask_field` 风格），回车保持当前值。

## 非目标

- 不做按平台各自独立的并发数（保持单一共享信号量）。
- 不持久化到 `gateways.json`。
- 不改动 asyncio 信号量跨多事件循环的既有行为（Feishu 每线程独立 loop 属现状）。

## 改动点

### 1. `gateway/runner.py`

- 新增 `set_max_concurrency(n: int) -> int`：
  - 校验 `n >= 1`（`< 1` 抬到 `1`）。
  - 重新绑定模块全局 `_GATEWAY_SEMAPHORE = asyncio.Semaphore(n)`。
  - 记录模块级 `_CURRENT_MAX = n`。
  - best-effort 调用 `feishu_ws.set_dispatch_limit(n)`：函数内**懒导入** `gateway.feishu_ws`，用 `try/except Exception` 包住，使未安装 lark / QQ-only 环境不报错。
  - 返回最终生效的 `n`（抬升后的值）。
- 新增 `current_max_concurrency() -> int`：返回 `_CURRENT_MAX`；模块加载时 `_CURRENT_MAX` 初值取 `max_concurrency()`（即环境变量默认）。
- `run_turn` 不改：它已是每次调用读模块全局 `_GATEWAY_SEMAPHORE`，rebind 后新 turn 自动用新信号量。
- `max_concurrency()`（读 env）保持不变，作为 `_CURRENT_MAX` 的初值来源。

### 2. `gateway/feishu_ws.py`

- 新增 `set_dispatch_limit(n: int) -> None`：重新绑定模块全局 `_dispatch_sem = threading.BoundedSemaphore(max(1, n))`。
- 现有 `_dispatch_sem` 的导入期初始化保持不变（兼容）。
- 现有 `with _dispatch_sem:` 调用点不改：每次进入读模块全局，rebind 后新 dispatch 用新信号量。

### 3. `orchestrator/repl_commands.py` 的 `_gw_start`

- 在调用 manager 启动**之前**插入并发输入：
  - 用 `rich.prompt.Prompt.ask`（与 `_ask_field` 同风格）弹出：
    `concurrency  [max simultaneous turns, 1 = serialized]  (current: N)`
    其中 `N = runner.current_max_concurrency()`，默认空、回车保持当前值。
  - 空输入（回车）= 保持当前值。
  - 非整数 / `< 1` = `render_command_error` 报错并**中止本次启动**（不启动、不改并发）。
- 解析成功后调 `runner.set_max_concurrency(n)`，再调用既有 `mgr.start_feishu(...)` / `mgr.start_qq(...)`。
- 启动成功提示追加一行并发信息，例如：
  `concurrency: 4 (parallel)` 或 `concurrency: 1 (serialized)`。

## 数据流

```
_gw_start
  -> runner.current_max_concurrency()         # 取当前值作默认
  -> Prompt.ask(...)                            # 用户输入（回车=保持）
  -> [非法] render_command_error + return       # 中止，不启动
  -> runner.set_max_concurrency(n)              # rebind asyncio + threading 两个信号量
  -> mgr.start_feishu/start_qq(...)             # 启动适配器
  -> render_text(..., "concurrency: n (...)")   # 提示含并发数
之后每个 turn:
  run_turn -> async with _GATEWAY_SEMAPHORE     # 新值
  feishu worker -> with _dispatch_sem           # 新值
```

## 错误处理

- `set_max_concurrency`：`n < 1` 抬到 `1`；`feishu_ws` 导入/设置失败时静默跳过（QQ 路径仍生效）。
- 输入框：非数字或 `< 1` → 报错并中止启动，既不启动也不改并发。

## 边界与取舍（已知、可接受）

- **全局单值的副作用**：若 QQ 已在跑（并发=3），之后再 Start Feishu 时输入的值会覆盖全局，连带影响 QQ 之后的新 turn。这是"全局一个数值"的预期行为；输入提示用 `(current: N)` 让用户看到当前值。
- rebind 只影响**之后**的 turn；正在进行的 turn 持有旧信号量对象、自然跑完。
- asyncio 信号量跨多事件循环的既有细节不在本次范围内。

## 测试

- `set_max_concurrency(n)` 后：`_GATEWAY_SEMAPHORE` 恰好放行 `n` 个并发持有者；`current_max_concurrency() == n`。
- `feishu_ws.set_dispatch_limit(n)` 后：`_dispatch_sem` 用 `acquire(blocking=False)` 恰好放行 `n` 个、第 `n+1` 个被拒。
- `set_max_concurrency` 在 `feishu_ws` 不可导入/设置失败时不抛错（QQ-only 路径）。
- `set_max_concurrency(0)` / 负数被抬到 `1`。
- `current_max_concurrency()` 初值反映 `GATEWAY_MAX_CONCURRENCY` 默认。
- （可选）`_gw_start` 输入解析：合法值调用 `set_max_concurrency` 并启动；非法值中止启动、不调用 manager。

现有测试（`tests/test_gateway/test_gateway_concurrency.py` reload 模块并直接引用 `_GATEWAY_SEMAPHORE` / `_dispatch_sem`）因沿用同名重绑定而不受影响。
