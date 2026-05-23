# 通讯智能体（comm-agent）设计稿

- 日期：2026-05-23
- 状态：待审稿
- 作者：`@沉沉` + Claude（pair design）

## 1. 背景与目标

项目现有三个 agent 角色（orchestrator / tool-agent / skill-agent），全部跑在同一台机器上，互相通过 127.0.0.1 上的 A2A HTTP + stdio MCP 通讯。

新需求：让本项目能与**另一台机器上的 agent**（首要对接 OpenClaw，未来兼容 Hermes Agent 等）互通：

- 委派任务给远端 agent（流式拿结果）
- 多轮对话（按 `context_id` 维持会话）
- 查询远端能力 / 状态

互操作性优先 → 采用 **Google A2A v0.3 开放标准**（JSON-RPC 2.0 over HTTP + SSE + `/.well-known/agent.json`）作为跨机协议，而不是扩展私有的 127.0.0.1 A2A。

新增 `comm-agent` 子进程承担这一职责，对内通过现有的 stdio MCP 把 `comm.*` 工具暴露给 orchestrator，对外通过 HTTPS + HMAC 与远端互连。

## 2. 架构与网络拓扑

```
┌─────────────────────────────────────────────────────────────────┐
│  Orchestrator（主进程）                                          │
│  ├──→ tool-agent      (现有，不动)                              │
│  ├──→ skill-agent     (现有，不动)                              │
│  └──→ comm-agent      (新增)                                    │
│         │ 对内：stdio MCP，把 comm.* 工具暴露给 orchestrator    │
│         │ 对外：监听 127.0.0.1:<eph> HTTP（标准 A2A v0.3）      │
│         ▼                                                       │
│       Caddy 子进程 (TLS + 自动 ACME)                            │
│         0.0.0.0:8443 → reverse_proxy → comm-agent ephemeral port│
└─────────────────────────────────────────────────────────────────┘
                              │  HTTPS (TLS 1.3, HMAC-signed JSON-RPC)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  远机（跑着 OpenClaw）                                          │
│  ├──→ OpenClaw 主进程                                           │
│  └──→ openclaw-a2a 插件（marketclaw-tech/openclaw-a2a）         │
│         /.well-known/agent.json + /a2a + /a2a/stream            │
│         ▼                                                       │
│       Caddy 子进程 (由我们的安装脚本配好)                       │
└─────────────────────────────────────────────────────────────────┘
```

### 关键设计决策

1. **comm-agent 是双向 endpoint**：既能主动 call 远端，也能被远端 call。A2A 标准要求 agent 双向对等。
2. **Caddy 是独立进程**：comm-agent 启动时模板渲染 Caddyfile 然后 `subprocess.Popen("caddy", "run", ...)`；退出时杀掉。自己实现 TLS + ACME 不值得。
3. **不复用现有 `agents/shared/a2a_server.py`**：它绑 127.0.0.1、无 agent card、无 task lifecycle。新建 `agents/comm_agent/a2a_protocol.py` 实现完整 spec，避免影响 tool-agent / skill-agent。
4. **peer 注册显式**：远端 URL + peer_id + 共享密钥写在 `comm_peers.json`（类似 `gateways.json`）。不做 mDNS / DHT 发现。

## 3. 组件结构

### 3.1 代码布局

```
agents/comm_agent/
├── __init__.py
├── main.py              # 子进程入口（python -m agents.comm_agent.main）
├── a2a_protocol.py      # 标准 A2A v0.3 客户端 + 服务端
├── agent_card.py        # 我方 Agent Card 构造 + JSON schema 校验
├── peer_registry.py     # 读写 .langchain-agent/comm_peers.json
├── caddy_supervisor.py  # 渲染 Caddyfile + 起 Caddy 子进程 + 关闭
└── mcp_tools.py         # 把 comm.* 工具暴露给 orchestrator（MCP stdio）

.agent/agents/
└── comm-agent.card.json   # 项目内 card（让 orchestrator 发现本 agent）

scripts/
├── install_openclaw_a2a.sh    # 远机用 Bash 安装脚本（Linux）
└── install_openclaw_a2a.ps1   # 远机用 PowerShell 版本（Windows）

tests/test_comm_agent/
├── test_a2a_protocol.py
├── test_peer_registry.py
├── test_agent_card.py
├── test_caddy_supervisor.py
├── test_e2e_loopback.py
├── test_e2e_auth_refuse.py
├── test_e2e_replay_blocked.py
└── test_e2e_stream_truncated.py

tests/test_e2e_multi_agent/
├── test_e2e_comm_delegate.py
└── test_e2e_comm_chat_multiturn.py
```

### 3.2 项目内 Card（`.agent/agents/comm-agent.card.json`）

```json
{
  "id": "comm-agent",
  "display_name": "Communication Specialist",
  "version": "1.0.0",
  "entrypoint": {
    "type": "python",
    "module": "agents.comm_agent.main",
    "args": []
  },
  "mcp": { "transport": "stdio" },
  "a2a": { "transport": "http", "port_strategy": "ephemeral", "streaming": true },
  "capabilities_hint": ["comm", "comm.delegate", "comm.chat", "comm.status"],
  "optional": true,
  "model_override": null
}
```

`optional: true` 让 orchestrator 在 comm-agent 启动失败（典型：caddy 不在 PATH）时仍能正常起 REPL，不阻塞用户使用其他 specialist。

### 3.3 对外暴露的 Agent Card（`/.well-known/agent.json`）

```json
{
  "schemaVersion": "0.3",
  "name": "agent-last-comm",
  "description": "Communication agent for the agent-last multi-agent system",
  "url": "https://<your-public-host>:8443",
  "version": "1.0.0",
  "provider": {
    "organization": "agent-last",
    "url": "https://github.com/<you>/agent-last"
  },
  "capabilities": {
    "streaming": true,
    "pushNotifications": false,
    "stateTransitionHistory": false
  },
  "authentication": {
    "schemes": ["HMAC-SHA256"],
    "credentials": "see install instructions"
  },
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["text/plain", "application/json"],
  "skills": [
    {
      "id": "task.delegate",
      "name": "Delegate a task",
      "description": "Hand off a free-form task to this agent; returns SSE stream of progress + final result",
      "tags": ["delegation", "task"],
      "inputModes": ["text/plain"],
      "outputModes": ["text/plain"]
    },
    {
      "id": "chat.message",
      "name": "Send chat message",
      "description": "Append a turn to a chat session (context_id-keyed)",
      "tags": ["chat", "multi-turn"]
    },
    {
      "id": "status.query",
      "name": "Query status",
      "description": "Return current agent state + tool inventory"
    }
  ]
}
```

### 3.4 暴露给 orchestrator 的 MCP 工具 surface

| 工具 | 入参 | 出参 | 说明 |
|------|------|------|------|
| `comm.list_peers` | — | `[{peer_id, name, url, last_seen, status}]` | 列出注册表里所有远端 |
| `comm.add_peer` | `peer_id, url, hmac_secret_value, display_name?` | `{ok, fetched_card, env_var_name}` | 注册新远端。**入参 `hmac_secret_value` 是密钥的实际值**；工具会把它写入一个新的 env var（如 `COMM_PEER_<PEER_ID>_HMAC`），只把 env-var **名**存进 JSON `hmac_secret_ref`。返回时告知用户分配的 env var 名，以便后续重启时重新设置。自动拉一次 agent card 校验连通性。 |
| `comm.remove_peer` | `peer_id` | `{ok}` | 删除注册项；不主动清除对应的 env var |
| `comm.peer_card` | `peer_id` | `<agent card JSON>` | 实时拉远端 card |
| `comm.delegate` | `peer_id, task, context?, stream?=true` | `stream=true` 返回 SSE 流 `{type, message/result, ...}`；`stream=false` 返回 `{final_result, events_count, duration_ms}` | 委派任务 |
| `comm.chat` | `peer_id, context_id, message` | `{reply, context_id}` | 多轮对话；首次可不传 context_id，服务端分配 |
| `comm.status` | `peer_id` | `{state, current_task?, last_error?}` | 查询远端当前状态 |

`comm.delegate` 的 SSE 流与现有 `tool.task` 完全同构 → orchestrator 现有 `stream_mux` 转发逻辑直接复用，无需新代码。

### 3.5 Peer 注册表 schema（`.langchain-agent/comm_peers.json`）

```json
{
  "schemaVersion": 1,
  "peers": [
    {
      "peer_id": "openclaw-home",
      "display_name": "OpenClaw @ home server",
      "url": "https://home.example.com:8443",
      "hmac_secret_ref": "OPENCLAW_HOME_HMAC",
      "tls": { "verify": true, "pinned_sha256": null },
      "added_at": "2026-05-23T...",
      "last_seen": "2026-05-23T..."
    }
  ]
}
```

约定：

- `hmac_secret_ref` 是**环境变量名**，密钥不写进 JSON（沿用 `gateways.json` 的处理方式）
- `tls.pinned_sha256` 在远端用自签证书时填，做证书指纹固定，跳过 CA 链
- **不允许** `tls.verify=false`（完全跳过 TLS 校验）

### 3.6 远端安装脚本

形态：单文件 Bash 脚本（外加 PowerShell 等价版本），幂等，参数化。

用户在远机执行：

```bash
curl -sSL https://raw.githubusercontent.com/<you>/agent-last/main/scripts/install_openclaw_a2a.sh \
  | bash -s -- \
      --my-peer-id openclaw-home \
      --your-peer-id agent-last-laptop \
      --public-host home.example.com \
      --hmac-secret "$(openssl rand -hex 32)"
```

脚本流程（每步幂等）：

1. 检测 OpenClaw 安装位置（env / 默认路径 / 询问）
2. 安装 OpenClaw A2A 插件：`openclaw skill install marketclaw-tech/openclaw-a2a@v0.3.x`（版本号固定）
3. 在 OpenClaw 配置目录写入 A2A 配置：
   ```yaml
   a2a:
     my_peer_id: openclaw-home
     hmac_secret_env: A2A_HMAC_SECRET
     allowed_peers:
       - peer_id: agent-last-laptop
         hmac_secret_env: A2A_HMAC_SECRET
   ```
4. 把 HMAC 密钥写进 systemd unit 或 `.env`（不裸放 shell history），同一份密钥**打印一次**让用户拷到主机端配 `OPENCLAW_HOME_HMAC`
5. 渲染 `/etc/caddy/Caddyfile.d/openclaw-a2a.caddy`
6. `systemctl reload caddy`（若未安装则先 `curl ... | bash` 装官方版，需交互确认）
7. 自测：`curl -sk https://localhost:8443/.well-known/agent.json` 打印 card 摘要
8. 打印一行给用户：在主机端 `comm.add_peer peer_id=openclaw-home url=https://home.example.com:8443 hmac_secret=<上面那串>`

Windows 远机用 PowerShell 版（脚本结构等价）。

## 4. 数据流：用户委派任务给 OpenClaw 的完整路径

```
1. 用户在 REPL 输入："让 openclaw-home 列出它能用的工具"
   │
   ▼
2. 主智能体决定调用 comm.delegate
   { peer_id: "openclaw-home", task: "列出你能用的工具", stream: true }
   │
   ▼ (stdio MCP)
3. comm-agent 子进程接到调用
   3a. peer_registry → url + hmac_secret_ref
   3b. authz.sign() 生成 grant：
       { peer_id: "agent-last-laptop",
         target_peer_id: "openclaw-home",
         requested_skill: "task.delegate",
         nonce: <16B random hex>,
         exp: now + 60s }
   3c. POST https://home.example.com:8443/a2a/stream
       { "jsonrpc": "2.0", "id": "...",
         "method": "message/stream",
         "params": {
           "message": { "role": "user", "parts": [{"text": "..."}] },
           "_meta": { "authz_grant": "<base64-jwt-like>" } } }
   │
   ▼ (HTTPS + TLS 1.3，经 Caddy)
4. 远端 OpenClaw 插件接到 POST /a2a/stream
   4a. 解 grant → 验签 → 验 nonce 未重放 + exp 未过
   4b. 把 message 喂给 OpenClaw 主进程
   4c. 边跑边 yield 事件 → 标准 A2A SSE：
       data: {"type":"task","state":"working","message":"..."}
       data: {"type":"artifact","name":"tools.json","parts":[...]}
       data: {"type":"task","state":"completed","result":"..."}
   │
   ▼ (SSE)
5. comm-agent 透传 SSE → orchestrator（通过 MCP 流式返回，tool.task 已有的机制）
   │
   ▼
6. stream_mux 把事件渲染到 REPL（现有逻辑）
   │
   ▼
7. 主智能体收到 final result，组织成回答给用户
```

## 5. 错误处理矩阵

| 失败点 | 表现 | 处理 |
|--------|------|------|
| peer 不在注册表 | `comm.delegate` 时 `peer_id` 找不到 | 工具返回 `{error: "unknown peer 'X'; run comm.add_peer first"}`，不抛异常 |
| HMAC env var 没设 | `hmac_secret_ref` 指向的 env 为空 | 同上，明确错误消息提示用户设环境变量 |
| DNS / TCP 连不上 | `httpx.ConnectError` | 重试 3 次，指数退避 0.5s/1s/2s，最后返回 `{error: "peer unreachable: ...", retried: 3}` |
| TLS 失败 | 证书过期 / 不可信 | 立即失败，不重试 |
| 远端 HTTP 4xx (401/403) | grant 被拒 | 不重试，返回 `{error: "auth refused: ..."}` |
| 远端 HTTP 5xx | 远端崩了 | 重试 3 次（临时故障），失败后返回 |
| SSE 中途断开 | socket 关掉 | 已收到 events 全部返回 + 追加 `{type:"error", message:"stream truncated after N events"}`。**不重连**（任务状态在远端已不确定） |
| grant 过期 | exp 超时 | 60s 内不会触发；SSE 流建立后用同一 grant，60s 足够 |
| Caddy 没装 / 启不来 | comm-agent 启动检测失败 | comm-agent 启动失败并退出，orchestrator 显示错误（`optional: true` 不阻塞 REPL） |
| 远端 agent card 拉不到 | `comm.add_peer` 时 fetch 失败 | 仍允许添加（card 是软依赖），`last_seen` 置 null，记录错误 |
| OpenClaw 自身崩了 | 远端 200 OK 但 SSE 出现 `{type:"error"}` | 透传给主智能体，由它决定要不要重试 |

## 6. 安全细节

### 6.1 HMAC grant 结构

扩展现有 `agents/shared/authz.py`，新增跨机字段：

```python
@dataclass
class CrossMachineGrant:
    peer_id: str           # 我方 peer_id（调用方自报家门）
    target_peer_id: str    # 目标 peer_id（防 grant 被劫持转发）
    requested_skill: str   # A2A skill id（task.delegate / chat.message / ...）
    nonce: str             # 16 字节 hex，防重放
    exp: int               # unix timestamp
```

签名格式：`base64url(JSON(claims)) + "." + base64url(HMAC-SHA256(payload))`。

**双写**：HTTP header `Authorization: A2A-HMAC <grant>` **AND** body 的 `params._meta.authz_grant`（方便不同框架的解析路径）。

### 6.2 防重放

远端用内存 LRU（容量 10000，TTL 60s）缓存最近见过的 nonce，重复即拒。

### 6.3 密钥轮转

注册表里 `hmac_secret_ref` 是 env var 名，轮转 = 改 env + 重启 comm-agent + 重启远端 Caddy/OpenClaw。**不做自动协商轮转**。

### 6.4 TLS 信任模型

- **首选**：公网域名 + Caddy ACME 拿 Let's Encrypt 证书，`tls.verify=true`，走标准 CA 链
- **备选**：自签证书 + 指纹固定。`comm.add_peer` 时若 verify 不过，提示用户 SSH 到远机取 `openssl x509 -fingerprint -sha256` 输出，填进注册表 `tls.pinned_sha256`
- **禁止** `tls.verify=false`（完全跳过校验）

### 6.5 Caddyfile 模板（主机端，comm-agent 启动时生成）

```caddyfile
# 自动写到 .langchain-agent/caddy/comm-agent.caddy
{$PUBLIC_HOST_OR_AUTO}:8443 {
    reverse_proxy localhost:{$COMM_AGENT_PORT}
    log {
        output file .langchain-agent/caddy/access.log
        format json
    }
}
```

`PUBLIC_HOST_OR_AUTO`：用户在 `.env` 里设；没设就用 `:8443`，Caddy 会颁内部证书（只适合 LAN/VPN 场景）。

### 6.6 任务持久化的边界

A2A 的 `task_id` **不在主机端持久化**：任务跑完就忘。理由：任务持久化语义复杂（重连 / 重放 / 状态恢复），用户实际诉求是"让远端帮我跑一下 X 然后给结果"，无状态足够。

`context_id` 不一样 —— 多轮对话需要，持久化在主机端注册表 `peers[].active_contexts[peer_id]`。会话级，不是任务级。

## 7. 测试策略

### 7.1 单元测试（无网络）

| 文件 | 覆盖 |
|------|------|
| `test_a2a_protocol.py` | JSON-RPC 编解码 / SSE 行分帧 / grant 签验 / nonce LRU |
| `test_agent_card.py` | Card schema 校验 / A2A v0.3 字段拼写 |
| `test_peer_registry.py` | CRUD / 并发写 / env-var-ref 解析 / 密钥不落盘 |
| `test_caddy_supervisor.py` | Caddyfile 渲染 / caddy 不在 PATH 时的报错 / 子进程退出回收（mock subprocess） |

### 7.2 集成测试（本机两个 comm-agent 互连）

| 文件 | 覆盖 |
|------|------|
| `test_e2e_loopback.py` | 起两个 comm-agent（18443/18444），互注册，跑 delegate/chat/status，verify 流式事件 + final result |
| `test_e2e_auth_refuse.py` | 错的 HMAC → 远端 401 + 正确错误消息 |
| `test_e2e_replay_blocked.py` | 重发同一 grant → 第二次被拒 |
| `test_e2e_stream_truncated.py` | 中途 kill server → 客户端收到 `{type:"error", message:"stream truncated"}` |

**关键**：loopback 测试**不用真 Caddy**，让两个 comm-agent 直接监听 HTTPS 端口（`trustme` 库生 ephemeral 自签 cert + key + CA + pinned fingerprint），避免 CI 装 caddy。Caddy 代码路径在 `test_caddy_supervisor.py` 用 mock subprocess 覆盖，外加手动 `make smoke-caddy` 目标不进 CI。

### 7.3 跨进程测试

| 文件 | 覆盖 |
|------|------|
| `test_e2e_comm_delegate.py` | orchestrator spawn comm-agent，主智能体调用 `comm.delegate`，对端 = 另一个 loopback comm-agent。verify SSE 经 stdio MCP → stream_mux 透传 |
| `test_e2e_comm_chat_multiturn.py` | 三轮对话，verify context_id 续传 |

### 7.4 真·跨机测试

**不进 CI**。`make smoke-real-peer` 目标，需要 `SMOKE_PEER_URL` + `SMOKE_PEER_HMAC` 才跑。本地手测 OpenClaw 对接时用。

### 7.5 测试基础设施

- `pytest-asyncio`（项目已有）
- 自签证书用 `trustme` 库（新依赖，轻量）
- mock 远端用**手写 `MockA2APeer`**（FastAPI app fixture），不用 respx —— SSE 流 + auth 流程更接近生产

## 8. 实施风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| OpenClaw 插件接口不稳定 | 安装脚本可能在升级后失效 | 固定插件版本 `@v0.3.x`；CI 不跑真 OpenClaw；升级 OpenClaw 后用户重跑脚本 |
| A2A v0.3 spec 演化 | 字段被未来 spec 弃用 | Card 写明 `schemaVersion: "0.3"`；解析对方 card 时只校验用到的字段（forward-compat） |
| Caddy 在 Windows 行为差异 | PS 脚本要单独验 | PS 脚本单独写 + 手测；CI 跑 Linux |
| HMAC 密钥泄露 | grant 可伪造 | 文档强调不进 git / 日志；grep `agents.shared.authz` 确认没有 `log.info(grant=...)`；安装脚本生成密钥只打印一次 |
| comm-agent 启 caddy 失败拖死整个系统 | 起 REPL 就崩 | card.json 标 `optional: true`，orchestrator spawn 逻辑容忍 specialist 启动失败（已有能力） |
| 公网 0.0.0.0:8443 被扫描 / DDoS | 远机被打爆 | 文档建议远机用 cloudflare tunnel 或限定来源 IP；不在本设计强制 |
| 自签证书的测试时生成 | CI 每跑都要生 | `trustme` ephemeral，跑完即弃 |

## 9. 范围之外（明确不做）

- mDNS / 自动发现
- 群播 / 一对多广播
- A2A push notifications（webhook 回调）
- 任务持久化 / 断线重连
- 会话历史归档
- 密钥自动协商 / 轮转
- 非 OpenClaw 远端的适配器（后续可加）
- 同一 comm-agent 并行委派多个 peer
- 审计日志 / 安全事件流
- 配额 / 限流
- TLS 叶证书 SHA-256 指纹固定（pin）强制校验 —— MVP 仅把 `tls_pinned_sha256` 当作"信任此自签名证书"的开关，完整指纹比对推迟到 v1.1；在此之前依赖 HMAC 对载荷签名来抵御 MITM（攻击者无法伪造签名）。

## 10. 实施工作量预估

- comm-agent 核心 (`a2a_protocol`/`agent_card`/`peer_registry`/`caddy_supervisor`/`mcp_tools`)：~600-800 行
- `main.py` 入口胶水：~100-150 行
- 安装脚本 bash + ps1：~150 + ~150 行
- 测试：~600-800 行
- 文档：~200 行

估实施时间 1-2 天专注开发（不含真·跨机调试的边角问题）。
