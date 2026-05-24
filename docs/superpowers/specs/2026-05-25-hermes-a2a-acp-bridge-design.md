# Hermes A2A↔ACP 跨机桥接设计稿

- 日期：2026-05-25
- 状态：已批准设计，待写实现计划
- 作者：`@沉沉` + Claude（pair design）
- 相关：[`2026-05-23-comm-agent-design.md`](2026-05-23-comm-agent-design.md)（comm-agent 跨机 A2A 基座）

## 1. 背景与目标

`comm-agent` 让 agent-last 能跨机委派任务给会说 **Google A2A v0.3** 的远端 agent（已对接 OpenClaw）。现在要对接 **Hermes**（NousResearch/hermes-agent）。

核对 Hermes 源码（`D:\Claude Code\hermes-agent`）后确认：Hermes **不说 A2A**。它能被外部程序驱动的接口只有两个，且都是 **stdio**：

| 接口 | 命令 | 传输 | 暴露 | 适合委派任务？ |
|---|---|---|---|---|
| **ACP**（Agent Client Protocol，Zed/VS Code 那套） | `hermes acp` / `python -m acp_adapter` | stdio JSON-RPC | 完整 Hermes AIAgent（chat / 工具 / 流式 / 审批） | ✅ 正是干这个的 |
| **MCP serve** | `hermes mcp serve` | stdio | 仅消息网关桥（收发 IM 消息），非任务委派 | ❌ |

且本次对接是**跨机**（Hermes 在另一台机器）。stdio ACP 本身不过网。

**目标**：在 Hermes 机器上跑一个 **A2A↔ACP 桥接进程**——对外说 comm-agent 现成的 A2A v0.3，对内 spawn `hermes acp` 用 stdio ACP 驱动 Hermes。这样 **agent-last 侧零改动**，配个 peer 即可委派任务 / 多轮对话 / 查状态。

**非目标**：改动 comm-agent / `A2AClient` / `comm.*` 工具 / peer 注册表；让 Hermes 原生支持 A2A；把桥接做成 Hermes 插件。

## 2. 架构与网络拓扑

```
agent-last 机器                          Hermes 机器
┌──────────────┐                  ┌───────────────────────────────────┐
│ comm-agent   │   HTTPS + HMAC   │  Caddy:8443 (安装脚本管理)         │
│ A2AClient    │ ───────────────► │    └─reverse_proxy→ 127.0.0.1:NN   │
│ (不改)       │   A2A v0.3 SSE   │  hermes-a2a 桥接 (uvicorn, 本地HTTP)│
└──────────────┘                  │    ├ build_app()  ← 复用 agent-last │
                                   │    └ ACP 客户端 ─stdio─► `hermes acp`│
                                   └───────────────────────────────────┘
```

关键决策：

1. **复用同源代码（路线 A）**：桥接 import agent-last 现成的 A2A 服务端（`build_app`）+ 鉴权（`authz`）+ 卡片（`agent_card`），只新增"ACP 客户端 + dispatcher"。保证 A2A 线格式 / grant 签验 / 防重放 / SSE 分帧与 comm-agent **逐字节同源**，零协议漂移。
2. **Caddy 由安装脚本管理**（与 openclaw 脚本一致）：桥接进程只在本地端口跑纯 HTTP，TLS / 反代交给 Caddy。桥接自身**不**起 Caddy 子进程（区别于 agent-last 本机 comm-agent 的 `main.py`）。
3. **agent-last 代码下放到远端**：安装脚本在 Hermes 机器 `git clone` agent-last（已存在则复用），桥接以 `python -m bridge.hermes_a2a` 运行。
4. **一个常驻 ACP 连接，多 session 复用**：ACP 原生支持一条 stdio 连接上跑多个并发 session。桥接懒启动一条 `hermes acp` 连接，按 A2A 语义开/复用 session。

## 3. 组件结构

### 3.1 代码布局（agent-last 仓库新增）

```
bridge/
└── hermes_a2a/
    ├── __init__.py
    ├── __main__.py        # 入口：读 env → build_app(dispatchers) → uvicorn
    ├── acp_client.py      # spawn `hermes acp`，ACP 客户端连接，session/prompt/cancel
    └── dispatchers.py     # ACP→A2A 翻译：stream_dispatcher + skill_dispatcher

scripts/
├── install_hermes_a2a.sh     # Linux/macOS 安装脚本
└── install_hermes_a2a.ps1    # Windows 安装脚本

tests/test_bridge_hermes/
├── __init__.py
├── conftest.py               # 假 ACP agent stub fixture
├── test_dispatchers.py       # ACP 事件序列 → A2A 事件断言
├── test_acp_client.py        # 连接/prompt/翻译/cancel（mock 子进程）
└── test_e2e_bridge.py        # 真 A2AClient → build_app → 假 ACP（ASGITransport，进 CI）
```

**复用（import，不重写）**：
- `agents.comm_agent.a2a_protocol.build_app`
- `agents.comm_agent.agent_card.build_self_card`
- `agents.shared.authz`（被 `build_app` 内部使用）

### 3.2 `build_app` 契约（已存在，不改）

```python
build_app(*, self_card, hmac_secret, my_peer_id,
          skill_dispatcher, stream_dispatcher, nonce_cache=None)
# skill_dispatcher : Callable[[method, params, claims], Awaitable[dict]]
# stream_dispatcher: Callable[[method, params, claims], AsyncIterator[dict]]
```

注意 dispatcher 收到的是**原始 method**（非 skill id）。路由：
- `message/stream` → `stream_dispatcher`（task.delegate）
- `message/send` → `skill_dispatcher`（chat.message）
- `status/query` → `skill_dispatcher`（status.query）

`build_app` 已负责：agent card 路由、grant 双写解析、HS256 验签、`target_peer_id`/`requested_skill` 校验、nonce 防重放、SSE `data: {json}\n\n` 分帧。

### 3.3 ACP 客户端（`acp_client.py`）

驱动 Hermes 的 ACP 生命周期（参考 Hermes 自带的 `agent/copilot_acp_client.py`，那是 Hermes 当 ACP 客户端去驱动别的 agent 的现成实现）：

```
spawn `hermes acp` (stdio 子进程)
  → initialize(protocol_version, client_capabilities, client_info)
  → 若 InitializeResponse.auth_methods 非空：authenticate(method_id=该 provider)
  → new_session(cwd=<HERMES_A2A_WORKDIR>) → session_id
  → prompt(prompt=[TextContentBlock(text)], session_id)
       ← 期间 Hermes 推 session/update 通知到客户端回调
       ← 返回 PromptResponse(stop_reason, usage)
  → cancel(session_id)  # 需要时
```

桥接实现 ACP **客户端侧**回调（`acp.Client`）：
- `session_update`：收到 `agent_message_chunk` / `tool_call`(start/complete) / `reasoning` / `usage_update` 等 → 翻译后入 `asyncio.Queue`。
- `request_permission`：远端无人审批。**默认拒**（返回 deny）；`HERMES_A2A_AUTO_APPROVE=1` 时放行。被拒时回放一条 `tool_result` 说明。

对外提供：
- `async ensure_session(context_id: str | None) -> str`：无 context_id 则 `new_session`，返回 session_id；有则复用（不存在则新建）。
- `async run_prompt(session_id, text) -> AsyncIterator[dict]`：跑一轮，产出**已翻译的 A2A 事件**；末尾产出 `completed`/`failed`。
- `running_sessions() -> dict`：供 status 查询。
- 子进程死亡时懒重连。

### 3.4 Dispatcher（`dispatchers.py`）

`make_dispatchers(acp_client, *, allowed_peer: str | None)` 返回 `(skill_dispatcher, stream_dispatcher)`。

可选纵深防御：若设了 `allowed_peer`，校验 `claims["peer_id"] == allowed_peer`，否则返回错误/拒绝。（`build_app` 已在共享密钥层 gate，此为额外加固。）

## 4. 协议映射

### 4.1 `task.delegate`（`message/stream`，流式）→ `stream_dispatcher`

```
1. text = params["message"]["parts"][0]["text"]; context = params.get("context_id")
2. yield {"type":"task","state":"working","message":"delegating to hermes"}
3. session_id = await acp_client.ensure_session(context)   # delegate 一般用临时 session
4. async for ev in acp_client.run_prompt(session_id, text):
       # agent_message_chunk → {"type":"text","text": chunk}
       # tool_call start/complete → {"type":"tool_call"/"tool_result", ...}
       # reasoning → {"type":"thinking","text": ...}
       yield ev
       （累积最终文本）
5. 正常结束 → yield {"type":"task","state":"completed","result": <最终文本>}
   异常/取消 → yield {"type":"task","state":"failed","error": ...}
```

并发：`run_prompt` 内 `create_task(prompt(...))`，`session_update` 回调把事件塞进 `asyncio.Queue`，生成器 `await queue.get()` 直到 done 哨兵；`PromptResponse` 返回后入 `completed` + 哨兵。

agent-last 侧 `comm.delegate` 收全部事件，从 `state=completed` 取 `result` —— 契约已对齐。

### 4.2 `chat.message`（`message/send`，同步）→ `skill_dispatcher`

```
1. msg = params["message"]["parts"][0]["text"]; context = params.get("context_id")
2. session_id = await acp_client.ensure_session(context)    # 首轮 context=None → 新建
3. 跑一轮 prompt，收敛最终 agent 文本 reply（流式事件丢弃，只要终值）
4. return {"reply": reply, "context_id": session_id}
```

agent-last 侧 `comm.chat` 读 `reply` + `context_id`，下一轮带回 `context_id` → 映射回同一 ACP session，多轮续接。

### 4.3 `status.query`（`status/query`，同步）→ `skill_dispatcher`

```
return {"state": "working" if 有 session 在跑 else "idle",
        "current_task": <在跑的 prompt 文本截断>,
        "model": <当前 ACP session 模型，可空>,
        "last_error": <最近一次错误，可空>}
```

ACP 无原生 status 方法；状态由桥接自记（它就是发起 prompt 的一方）。

## 5. 鉴权与配置（桥接环境变量）

| 变量 | 默认 | 作用 |
|---|---|---|
| `HERMES_A2A_MY_PEER_ID` | `hermes-home` | 桥接自身 peer_id；grant `target_peer_id` 必须等于它 |
| `HERMES_A2A_HMAC` | —（必填） | 与 agent-last 共享的 HMAC 密钥；`build_app` 用它验 grant |
| `HERMES_A2A_ALLOWED_PEER` | 空 | 可选：校验 grant 调用方 `peer_id`（纵深防御） |
| `HERMES_A2A_PORT` | `19444` | 桥接本地监听端口（Caddy 反代到这里） |
| `HERMES_A2A_WORKDIR` | 临时目录 | ACP `new_session` 的 cwd（Hermes 文件/终端工具相对它） |
| `HERMES_ACP_CMD` | `hermes acp` | 启动 ACP 服务端的命令 |
| `HERMES_A2A_AUTO_APPROVE` | `0` | ACP 危险操作审批：默认拒，置 1 放行 |

`build_app` 在共享密钥层已 gate（密钥按 peer-pair 分配，只有持有者能签 grant），caller 白名单为可选加固。

## 6. 安装脚本 `scripts/install_hermes_a2a.{sh,ps1}`

对齐 openclaw 脚本结构。参数：`--my-peer-id` / `--your-peer-id` / `--public-host` / `--hmac-secret`；可选 env 覆盖：`--agent-last-repo`（克隆源）、`--hermes-acp-cmd`、`CADDY_PORT`、`HERMES_A2A_PORT`。

七步（每步幂等）：

1. **检查 `hermes` 在 PATH**（否则 `HERMES_BIN`）。
2. **确保 ACP 依赖**：`python -c "import acp"` 通过即可；否则提示在 Hermes 目录 `pip install -e '.[acp]'`。
3. **拉 agent-last**：`git clone <--agent-last-repo>` 到目标目录（已存在则 `git pull`）；装桥接依赖 `pip install fastapi uvicorn pyjwt httpx`（均在 agent-last requirements）。
4. **写 env 文件**（mode 600 / Windows ACL）：`HERMES_A2A_HMAC`、`HERMES_A2A_MY_PEER_ID`、`HERMES_A2A_ALLOWED_PEER=<your-peer-id>`、端口、`HERMES_ACP_CMD`。HMAC 写文件不裸放 history。
5. **渲染 Caddyfile**：`<public-host>:<CADDY_PORT> { reverse_proxy localhost:<HERMES_A2A_PORT> }`。
6. **起桥接 + reload Caddy**：systemd unit（或 nohup 回退）跑 `python -m bridge.hermes_a2a`；`systemctl reload caddy`（无 systemd 则提示手动 `caddy run`）。
7. **自测**：`curl -sk https://localhost:<CADDY_PORT>/.well-known/agent.json` 拉到 card 即成功；打印主机端要跑的：
   ```
   comm.add_peer peer_id=<my-peer-id> url=https://<public-host>:<CADDY_PORT> hmac_secret_value=<密钥>
   ```
   （密钥只打印一次。）

## 7. 错误处理矩阵

| 失败点 | 表现 | 处理 |
|---|---|---|
| `hermes acp` 起不来 | spawn 失败 | delegate→`{"type":"task","state":"failed","error":"hermes acp unavailable"}`；chat/status→`{"error":...}`；不崩进程 |
| `hermes acp` 中途崩 | stdio EOF / 子进程退出 | 当前轮标 failed；下次请求懒重连 |
| ACP prompt 超时 | 久不返回 | 受 agent-last `A2AClient` 默认 30s 限制（已知局限，沿用；长任务后台化范围外） |
| 危险工具审批 | `request_permission` | 默认拒 + 回放 `tool_result` 说明；`HERMES_A2A_AUTO_APPROVE=1` 放行 |
| grant 过期/重放/签名错 | — | 复用 `build_app` 401（不变） |
| `HERMES_A2A_HMAC` 没设 | 启动 | 启动即报错退出（脚本自测会暴露） |
| caller peer 不在白名单 | 设了 `ALLOWED_PEER` 且不符 | dispatcher 返回错误/拒绝 |
| SSE 中途断 | agent-last 侧 | 复用 `A2AClient` 的 `stream truncated` 标记（不变） |

## 8. 测试策略

### 8.1 单元（无网络、无真 Hermes）
- `test_dispatchers.py`：喂假 `session/update` 序列 → 断言翻译出的 A2A 事件序列（含 `completed`/`failed`）；chat 的 `context_id` 复用与隔离；status 的 idle/working 映射；`allowed_peer` 校验。
- `test_acp_client.py`：mock `hermes acp` 子进程，验 initialize→(authenticate)→new_session→prompt→cancel 流程、子进程死亡懒重连。

### 8.2 集成（进 CI）
- `test_e2e_bridge.py`：用 agent-last 现成的 `httpx ASGITransport` 注入手法，让**真 `A2AClient`** 打桥接 `build_app`，ACP 端接一个**假 ACP agent stub**（发 canned 通知）。验证 delegate 流式、chat 多轮 context_id、status，全程不连真 Hermes。

### 8.3 真 Hermes 冒烟（不进 CI）
- `make smoke-hermes`：需 `SMOKE_HERMES=1`，本地手测真 `hermes acp`。

## 9. 交付物清单

- `bridge/hermes_a2a/`（`__main__.py` / `acp_client.py` / `dispatchers.py` / `__init__.py`）
- `scripts/install_hermes_a2a.sh` + `.ps1`
- `tests/test_bridge_hermes/`
- **README 更新**（本期约定并入本计划）：
  - `agents/comm_agent/README.md`：新增"对接 Hermes（A2A↔ACP 桥接）"章节（安装脚本用法 / ACP 映射 / 连接流程 / 限制），并把规划中的占位说明替换为正式文档。
  - 主 `README.md`：comm-agent 段补一句 Hermes 经桥接对接的指引链接。
- 可选：`requirements`/extra 里登记桥接远端依赖说明（不强加进主依赖）。

## 10. 范围之外（明确不做）

- Hermes 危险操作审批的人工回流（默认拒；`AUTO_APPROVE` 全放行二选一）。
- ACP image / resource 多模态块的透传（仅文本）。
- `comm.delegate` 长任务后台化（沿用同步 + 30s 超时局限）。
- 桥接进程的非 systemd 进程管理 / 自管 Caddy。
- 任务持久化 / 断线重连（沿用 comm-agent MVP 边界）。
- 让 Hermes 原生支持 A2A、或把桥接做成 Hermes 插件。
- agent-last 侧任何改动。

## 11. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| `acp`(agent-client-protocol) 客户端 API 版本差异 | 桥接连不上 | 参考 Hermes `agent/copilot_acp_client.py` 同款用法；固定/检测版本 |
| ACP `session/update` 事件形状随 Hermes 升级变 | 翻译漏事件 | 只依赖 `agent_message_chunk` + `PromptResponse` 终值这条最稳路径；tool/thinking 事件为增强、缺失不致命 |
| 远端要同时装 Hermes + agent-last 两套依赖 | 环境冲突 | 桥接依赖极少（fastapi/uvicorn/pyjwt/httpx）；建议独立 venv |
| 自动放行审批导致远端被远程驱动执行危险命令 | 安全 | 默认拒；放行需显式 `AUTO_APPROVE=1` 并在文档警示 |
| 真·跨机 TLS / 端口 | 连不上 | 沿用 Caddy ACME + 安装脚本自测；同 comm-agent 既有经验 |
