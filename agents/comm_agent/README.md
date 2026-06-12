# comm-agent — 跨机通信智能体使用手册

> 让本项目的主智能体（orchestrator）能跟**另一台机器上的 agent**（首要对接 OpenClaw，未来兼容 Hermes 等）互通：委派任务、多轮对话、查状态。
>
> 跨机协议采用 Google **A2A v0.3**（JSON-RPC 2.0 over HTTPS + SSE + `/.well-known/agent.json`），载荷用 **HMAC-SHA256** 签名，TLS 由 **Caddy** 终结。

本文档覆盖：它是什么、怎么在主机端跑起来、**怎么用安装脚本给远端装通信协议**、怎么注册并调用远端、环境变量参考、安全模型、密钥持久化与故障排查。

> 设计原稿见 [`docs/superpowers/specs/2026-05-23-comm-agent-design.md`](../../docs/superpowers/specs/2026-05-23-comm-agent-design.md)。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [架构与网络拓扑](#架构与网络拓扑)
- [对外暴露的工具 comm.*](#对外暴露的工具-comm)
- [快速开始（主机端）](#快速开始主机端)
- [给远端安装通信协议（重点）](#给远端安装通信协议重点)
- [注册远端并开始调用](#注册远端并开始调用)
- [环境变量参考](#环境变量参考)
- [安全模型](#安全模型)
- [密钥持久化与轮转](#密钥持久化与轮转)
- [故障排查](#故障排查)
- [当前限制与路线](#当前限制与路线)

---

## 它解决什么问题

主项目的三个角色（orchestrator / tool-agent / skill-agent）全部跑在同一台机器、走 `127.0.0.1` 上的 MCP + A2A。`comm-agent` 是第四个 specialist，专门负责**跨机**通信：

- **委派任务**（`comm.delegate`）：把一句话任务交给远端 agent，流式拿回进度和结果。
- **多轮对话**（`comm.chat`）：按 `context_id` 维持一段会话。
- **查能力 / 状态**（`comm.peer_card` / `comm.status`）：看远端是谁、现在在干啥。

它对内通过 stdio MCP 把 `comm.*` 工具暴露给 orchestrator，对外通过 HTTPS 与远端互连——**双向对等**：既能主动呼叫远端，也能被远端呼叫。

---

## 架构与网络拓扑

```
┌─────────────────────────────────────────────────────────────────┐
│  本机 Orchestrator（主进程）                                     │
│  ├──→ tool-agent / skill-agent   (本地 specialist，不变)         │
│  └──→ comm-agent                 (本手册主角)                    │
│         │ 对内：stdio MCP，把 comm.* 暴露给 orchestrator          │
│         │ 对外：监听 127.0.0.1:<随机端口> 跑 A2A v0.3 HTTP        │
│         ▼                                                         │
│       Caddy 子进程（TLS 终结 + 自动 ACME / 内部证书）            │
│         0.0.0.0:8443  ──reverse_proxy──►  comm-agent 随机端口     │
└─────────────────────────────────────────────────────────────────┘
                    │  出站：httpx 直连远端 URL（不经本机 Caddy）
                    │  入站：远端经本机 Caddy:8443 打进来
                    ▼  HTTPS（TLS 1.3）+ HMAC 签名的 JSON-RPC
┌─────────────────────────────────────────────────────────────────┐
│  远机（跑着 OpenClaw）                                           │
│  ├──→ OpenClaw 主进程                                            │
│  └──→ openclaw-a2a 插件                                          │
│         /.well-known/agent.json + /a2a + /a2a/stream             │
│         ▼                                                         │
│       Caddy 子进程（由我们的安装脚本配好）                       │
└─────────────────────────────────────────────────────────────────┘
```

**关键区分——出站 vs 入站**：

| 方向 | 谁发起 | 走哪条路 | 本机要不要 Caddy |
|---|---|---|---|
| **出站** `comm.delegate` / `comm.chat` / `comm.status` | 本机主智能体 | comm-agent 用 httpx **直连**远端 URL | **不需要** |
| **入站** 远端反过来呼叫本机 | 远端 agent | 远端 → 本机 Caddy:8443 → comm-agent | **需要**（且要配好密钥，见下） |

> 也就是说：如果你只想**委派任务给远端**、不打算被远端呼叫，本机连 Caddy 都不用装，`comm.delegate` 照样能用。

Caddy 起不来时 comm-agent 不会拖垮系统：它降级为"仅 stdio MCP"（出站工具仍可用，入站全部 401），并在日志里 `WARNING`。`.agent/agents/comm-agent.card.json` 里标了 `"optional": true`，spawn 失败也不阻塞 REPL。

---

## 对外暴露的工具 comm.*

comm-agent 通过 stdio MCP 暴露 7 个工具。**所有工具永不抛异常**——出错时返回 `{"ok": false, "error": "..."}` 的 JSON，让调用方的 LLM 能读到并自行决策。

| 工具 | 必填入参 | 可选入参 | 返回 |
|---|---|---|---|
| `comm.list_peers` | — | — | `{peers: [{peer_id, display_name, url, last_seen}]}` |
| `comm.add_peer` | `peer_id`, `url`, `hmac_secret_value` | `display_name` | `{ok, peer_id, env_var_name, fetched_card, note}` |
| `comm.remove_peer` | `peer_id` | — | `{ok, peer_id, removed}` |
| `comm.peer_card` | `peer_id` | — | `{ok, card}`（实时拉远端 `/.well-known/agent.json`） |
| `comm.delegate` | `peer_id`, `task` | `context`, `stream`(默认 `true`) | `stream=true`：`{ok, events, final_result, duration_ms}`；`stream=false`：`{ok, events_count, final_result, duration_ms}` |
| `comm.chat` | `peer_id`, `message` | `context_id` | `{ok, reply, context_id}` |
| `comm.status` | `peer_id` | — | `{ok, status}` |

几个要点：

- **`comm.add_peer` 的 `hmac_secret_value` 是密钥的明文值**。工具会把它写进一个**进程环境变量**（名字形如 `COMM_PEER_<PEER_ID>_HMAC`，由 `peer_id` 推导），注册表 JSON 里只存这个**变量名**（`hmac_secret_ref`），密钥本身不落盘。返回的 `note` 会提示你怎么持久化这个变量。
- `comm.add_peer` 会顺手拉一次远端 agent card 验证连通性；**拉不到也允许添加**（card 是软依赖，`last_seen` 置 null）。
- `comm.delegate` 的 SSE 事件类型有 `task`（含 `state: working/completed/failed`）、`artifact`、`text`、`error` 等，与本地 `tool.task` 同构，orchestrator 的 `stream_mux` 直接复用。
- `comm.chat` 首轮把 `context_id` 留空，服务端分配后返回，后续轮次带上它即可续接会话。

> **目前怎么触发这些工具？** 当前由 orchestrator 的 planner LLM 从自然语言识别意图后调用，例如在 REPL 里直接说"让 openclaw-home 帮我列一下它能用的工具"。显式的 `/comm`、`/task`、`/chat` 斜杠命令正在 `feat/comm-slash-commands` 分支开发中（见[当前限制与路线](#当前限制与路线)），本文档描述的是已落地的工具层。

---

## 快速开始（主机端）

### 1. 装依赖

```bash
pip install -r requirements.txt
```

### 2. 装 Caddy（仅当你需要**被远端呼叫**时）

参见 https://caddyserver.com/docs/install 。装好后确保 `caddy` 在 PATH 上（或用 `CADDY_BINARY` 指定路径）。纯出站委派可跳过这步。

### 3. 配置入站密钥（仅入站需要）

> ⚠️ **这里有个容易踩的坑**：`COMM_AGENT_SELF_HMAC` 存的是"**存放密钥的环境变量的名字**"，**不是密钥本身**（与注册表 `hmac_secret_ref` 同款间接设计）。直接 `export COMM_AGENT_SELF_HMAC=<一串随机hex>` 是**无效的**——它会把那串 hex 当成另一个变量名去查，查不到，入站全部 401。

正确做法是设**两个**变量：一个放名字，一个放值。

Linux / macOS：

```bash
# 1) 指定"密钥变量的名字"
export COMM_AGENT_SELF_HMAC=INBOUND_HMAC
# 2) 在那个名字下放真正的密钥
export INBOUND_HMAC=$(openssl rand -hex 32)
```

Windows PowerShell：

```powershell
$env:COMM_AGENT_SELF_HMAC = "INBOUND_HMAC"
$env:INBOUND_HMAC = -join ((48..57)+(97..102) | Get-Random -Count 64 | ForEach-Object {[char]$_})
```

把 `INBOUND_HMAC` 的值告诉远端，远端用它来签发对你的呼叫（HMAC 是对称密钥，两边一致）。

### 4. （可选）设公网主机名让 Caddy 自动签证书

```bash
export COMM_AGENT_PUBLIC_HOST=laptop.example.com   # 有公网域名时，Caddy 走 ACME 拿 Let's Encrypt 证书
# 不设则监听 :8443 + Caddy 内部自签证书，只适合 LAN / VPN
```

### 5. 启动 REPL

```bash
python cli.py
```

comm-agent 作为 specialist 随 orchestrator 自动 spawn（前提是 `.agent/agents/comm-agent.card.json` 存在）。启动后会：

- 在 `127.0.0.1:<随机端口>` 起 A2A FastAPI 服务；
- 渲染 `.langchain-agent/caddy/comm-agent.caddy` 并拉起 Caddy 子进程；
- 把对外 URL 写到 `.agent/runtime/comm-agent.a2a-url` 供 orchestrator 发现。

---

## 给远端安装通信协议（重点）

要让一台远程机器能被本项目呼叫，远端必须：装好 A2A 插件、配好 peer 白名单与共享密钥、用 Caddy 暴露 HTTPS 端点。我们提供了**一键安装脚本**把这些步骤打包。

脚本位置：

- `scripts/install_openclaw_a2a.sh` — Linux / macOS（Bash）
- `scripts/install_openclaw_a2a.ps1` — Windows（PowerShell）

### 前置条件（远端）

- 远端已安装 **OpenClaw**，且 `openclaw` 在 PATH 上（否则用 `OPENCLAW_BIN` 环境变量指向它）。
- 远端已安装 **Caddy**（脚本不负责装 Caddy，只生成配置并 reload；没有 systemd/服务时会提示你手动 `caddy run`）。
- 远端有公网可达的主机名 / 域名（`--public-host`），且 Caddy 端口（默认 8443）入站放行。

### 用法（Linux / macOS）

在**远程机器**上执行：

```bash
curl -sSL https://raw.githubusercontent.com/<your-repo>/main/scripts/install_openclaw_a2a.sh \
  | bash -s -- \
      --my-peer-id    openclaw-home \
      --your-peer-id  ww-agent \
      --public-host   home.example.com \
      --hmac-secret   "$(openssl rand -hex 32)"
```

### 用法（Windows / PowerShell）

```powershell
$secret = -join ((48..57)+(97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})
iex "& { $(iwr -useb https://raw.githubusercontent.com/<your-repo>/main/scripts/install_openclaw_a2a.ps1) } `
  -MyPeerId openclaw-home -YourPeerId ww-agent -PublicHost home.example.com -HmacSecret $secret"
```

### 参数说明

| 参数（bash / PowerShell） | 含义 | 必填 |
|---|---|---|
| `--my-peer-id` / `-MyPeerId` | **远端**自报的 peer_id，你将在主机端用它注册（如 `openclaw-home`） | ✅ |
| `--your-peer-id` / `-YourPeerId` | **本机** comm-agent 的 peer_id，写进远端白名单。必须等于本机 `COMM_AGENT_MY_PEER_ID`（默认 `ww-agent`） | ✅ |
| `--public-host` / `-PublicHost` | 远端对外的主机名 / 域名，写进远端 Caddyfile | ✅ |
| `--hmac-secret` / `-HmacSecret` | 双方共享的 HMAC 密钥。**建议现场用 `openssl rand -hex 32` 生成**，脚本结尾会原样回显一次让你拷到主机端 | ✅ |

可用环境变量覆盖默认值：`OPENCLAW_BIN`（默认 `openclaw`）、`A2A_PLUGIN_VERSION`（默认 `v0.3.0`）、`CADDY_PORT`（默认 `8443`）、`OPENCLAW_A2A_PORT`（默认 `19443`，OpenClaw 内部监听端口）、`CADDY_DIR`。

### 脚本干了什么（7 步，每步幂等）

1. **检查 OpenClaw** 是否在 PATH（不在则报错退出，提示装 OpenClaw 或设 `OPENCLAW_BIN`）。
2. **安装 A2A 插件**：`openclaw skill install marketclaw-tech/openclaw-a2a@v0.3.0`（版本固定，避免上游漂移）。
3. **写 A2A 配置**到 OpenClaw 配置目录（`openclaw config-dir` 或 `~/.openclaw`）下的 `a2a.yaml`：
   ```yaml
   a2a:
     my_peer_id: "openclaw-home"
     listen_port: 19443
     hmac_secret_env: A2A_HMAC_SECRET
     allowed_peers:
       - peer_id: "ww-agent"     # ← 只允许这个 peer 呼叫
         hmac_secret_env: A2A_HMAC_SECRET
   ```
4. **持久化密钥**：把 `A2A_HMAC_SECRET=<密钥>` 写进 `a2a.env`（Linux `chmod 600`；Windows 用 ACL 锁到当前用户），不裸放进 shell history。
5. **生成 Caddyfile**（`<caddy-dir>/openclaw-a2a.caddy`）：
   ```caddyfile
   home.example.com:8443 {
       reverse_proxy localhost:19443
   }
   ```
   站点地址带主机名时，Caddy 会自动走 ACME 申请并续期证书。
6. **reload Caddy**：检测到 systemd 下的 caddy 服务（或 Windows caddy 服务）就 reload / restart；否则提示你手动 `caddy run --config <path>`。
7. **自测**：`curl -sk https://localhost:8443/.well-known/agent.json`，能拉到 agent card 即成功，并打印下一步要在主机端跑的 `comm.add_peer` 命令。

脚本结尾会打印类似：

```
✅ Install complete.

Next step on your laptop:
  In the comm-agent REPL, register this peer:
    comm.add_peer peer_id=openclaw-home \
                  url=https://home.example.com:8443 \
                  hmac_secret_value=<刚才那串密钥>

(Keep that HMAC secret safe — it's the only copy printed.)
```

> **密钥只打印这一次**。务必当场拷走——它既要写进远端的 `a2a.env`（脚本已做），也要在主机端注册时作为 `hmac_secret_value` 传入。双方必须是**同一个值**（对称 HMAC）。

### 远端不是 OpenClaw 怎么办？

脚本目前只封装了 OpenClaw 的 `openclaw-a2a` 插件。对接**本身就会说 A2A v0.3** 的其它实现（如自写的 A2A 服务）时，远端只要满足下面的契约即可被本项目呼叫：

- 暴露 `GET /.well-known/agent.json`（schemaVersion `"0.3"`，含 `name/description/url/version/skills`）；
- 暴露 `POST /a2a`（JSON-RPC 同步）与 `POST /a2a/stream`（SSE）；
- 接受 `Authorization: A2A-HMAC <grant>` 头**或** body 的 `params._meta.authz_grant`（[双写](#安全模型)）；
- 用共享密钥按 HS256 校验 grant，并校验 `target_peer_id == 自己的 peer_id`、`requested_skill == 路由对应的 skill`、`nonce` 未重放、`exp` 未过期；
- 方法名到 skill 的映射：`message/stream → task.delegate`、`message/send → chat.message`、`status/query → status.query`。

照着 `agents/comm_agent/a2a_protocol.py` 的 `build_app()` 实现即可（它本身就是一份可参考的 A2A v0.3 服务端）。

> **Hermes 走 ACP，不说 A2A。** Hermes（NousResearch/hermes-agent）对外只暴露 **stdio 上的 ACP**，不满足 A2A 契约，不能直连。对接方式见下方「对接 Hermes（A2A↔ACP 桥接）」。

### 对接 Hermes（A2A↔ACP 桥接）

Hermes 那台机器上要跑一个**桥接进程**：对外说 comm-agent 的 A2A v0.3，对内 spawn 本地 `hermes acp` 用 stdio ACP 驱动 Hermes。W&W Agent 侧零改动——注册个 peer 就能用。

**前置（Hermes 机器）**：装好 Hermes 且 `hermes` 在 PATH；Hermes 的 ACP 依赖已装（`pip install -e '.[acp]'`）；装好 Caddy；有公网主机名。

**一键安装**（在 Hermes 机器执行）：

```bash
curl -sSL https://raw.githubusercontent.com/<your-repo>/main/scripts/install_hermes_a2a.sh \
  | bash -s -- \
      --my-peer-id    hermes-home \
      --your-peer-id  ww-agent \
      --public-host   home.example.com \
      --hmac-secret   "$(openssl rand -hex 32)"
```

Windows 用 `scripts/install_hermes_a2a.ps1`（参数同名）。脚本会：拉 W&W Agent（复用其 A2A 服务端模块）、装桥接依赖、写 `~/.hermes-a2a/bridge.env`、渲染 Caddyfile，并打印主机端要跑的 `comm.add_peer` 行。

**协议映射**：`task.delegate`→ACP `session/new`+`session/prompt`（流式）；`chat.message`→复用 ACP session（`context_id`↔`sessionId`）；`status.query`→桥接自记运行态。

**注册后照常用**：

```
comm.add_peer peer_id=hermes-home url=https://home.example.com:8443 hmac_secret_value=<密钥>
comm.delegate peer_id=hermes-home task="..."
comm.chat     peer_id=hermes-home message="..."
```

**限制**：危险操作审批默认**拒**（远端无人值守），放行需在桥接端设 `HERMES_A2A_AUTO_APPROVE=1`；仅透传文本（ACP image/resource 块本期不接）；受 `A2AClient` 30s 超时限制。

---

## 注册远端并开始调用

远端装好后，回到主机端的 REPL。

### 1. 注册远端（comm.add_peer）

把脚本打印的那行作为意图告诉主智能体（当前经 planner；斜杠命令落地后可直接 `/comm add`）。底层等价于：

```
comm.add_peer
  peer_id=openclaw-home
  url=https://home.example.com:8443
  hmac_secret_value=<远端脚本打印的密钥>
```

成功返回里会有：

```json
{
  "ok": true,
  "peer_id": "openclaw-home",
  "env_var_name": "COMM_PEER_OPENCLAW_HOME_HMAC",
  "fetched_card": { "...": "拉到的远端 agent card" },
  "note": "persist env var: export COMM_PEER_OPENCLAW_HOME_HMAC=<value> in your shell profile"
}
```

注册表落到 `.langchain-agent/comm_peers.json`，只存变量名不存密钥：

```json
{
  "schemaVersion": 1,
  "peers": [{
    "peer_id": "openclaw-home",
    "display_name": "openclaw-home",
    "url": "https://home.example.com:8443",
    "hmac_secret_ref": "COMM_PEER_OPENCLAW_HOME_HMAC",
    "tls": { "verify": true, "pinned_sha256": null },
    "added_at": "2026-05-24T...",
    "last_seen": "2026-05-24T..."
  }]
}
```

### 2. 委派任务（comm.delegate）

在 REPL 里说："让 openclaw-home 列出它能用的工具"。主智能体路由到：

```
comm.delegate peer_id=openclaw-home task="列出你能用的工具"
```

`stream=true`（默认）会把全部 SSE 事件经 stdio MCP 透传给 orchestrator，由 `stream_mux` 渲染到终端；最终结果在 `final_result`。

### 3. 多轮对话（comm.chat）

```
comm.chat peer_id=openclaw-home message="你好，你是谁？"     # 首轮，context_id 留空
# 返回 {reply, context_id}；下一轮带上 context_id 续接
comm.chat peer_id=openclaw-home message="那你能帮我做什么？" context_id=<上轮返回的>
```

### 4. 查状态 / 看 card

```
comm.status    peer_id=openclaw-home   # 远端当前在 idle 还是在跑任务
comm.peer_card peer_id=openclaw-home   # 实时拉远端能力清单
comm.list_peers                        # 列出所有已注册远端
```

---

## 环境变量参考

### 主机端（comm-agent 进程，见 `agents/comm_agent/main.py`）

| 变量 | 默认 | 作用 |
|---|---|---|
| `COMM_AGENT_MY_PEER_ID` | `ww-agent` | 本机自报家门的 peer_id，**必须等于远端白名单里的 `your-peer-id`** |
| `COMM_AGENT_PUBLIC_HOST` | 空 | 设了 → Caddy 走 ACME 给该域名签证书；不设 → 监听 `:8443` 用内部自签证书（仅 LAN/VPN） |
| `COMM_AGENT_PUBLIC_PORT` | `8443` | Caddy 对外监听端口（写进对外 URL 与 Caddyfile） |
| `COMM_AGENT_SELF_HMAC` | `COMM_AGENT_SELF_HMAC` | **存放入站密钥的环境变量的"名字"**（间接引用）。详见[快速开始第 3 步的坑](#3-配置入站密钥仅入站需要) |
| `CADDY_BINARY` | `caddy` | Caddy 可执行文件路径 / 名称 |
| `AGENT_ID` | `comm-agent` | 运行时 URL 文件名（`.agent/runtime/<AGENT_ID>.a2a-url`） |

注册每个远端时，`comm.add_peer` 还会写入一个 `COMM_PEER_<PEER_ID>_HMAC` 进程变量存该远端的出站密钥。

### 远端（安装脚本参数 / 环境变量）

| 变量 | 默认 | 作用 |
|---|---|---|
| `OPENCLAW_BIN` | `openclaw` | OpenClaw 可执行文件 |
| `A2A_PLUGIN_VERSION` | `v0.3.0` | 安装的 A2A 插件版本（固定） |
| `CADDY_PORT` | `8443` | 远端 Caddy 对外端口 |
| `OPENCLAW_A2A_PORT` | `19443` | OpenClaw A2A 插件内部监听端口（Caddy 反代到这里） |
| `CADDY_DIR` | `/etc/caddy/Caddyfile.d`（回退 `~/.caddy`） | Caddyfile 片段目录 |
| `A2A_HMAC_SECRET` | — | 远端共享密钥（脚本写进 `a2a.env`） |

---

## 安全模型

每一次跨机调用都带一个 **HMAC grant**——一个 60 秒过期的 JWT（HS256），claims 绑定到一次具体调用：

```jsonc
// agents/shared/authz.py :: sign_cross_machine_grant
{
  "peer_id":         "ww-agent",  // 调用方自报家门
  "target_peer_id":  "openclaw-home",      // 目标必须是它（防 grant 被劫持转发）
  "requested_skill": "task.delegate",      // 绑定到 A2A skill id，不是裸方法名
  "nonce":           "<16字节 hex 随机>",   // 防重放
  "exp":             1716284321            // unix 时间戳，签发后 60s
}
```

防御要点：

- **双写**：grant 同时放在 HTTP 头 `Authorization: A2A-HMAC <grant>` **和** body 的 `params._meta.authz_grant`，兼容不同框架的解析路径。
- **防重放**：服务端用一个 10000 条、TTL 60s 的内存 LRU（`NonceCache`）记住见过的 nonce，重复即 401。
- **防转发**：服务端校验 `target_peer_id` 等于自己的 peer_id，`requested_skill` 等于该路由对应的 skill，任一不符即拒。
- **最小重试**：连接 / 5xx 错误指数退避重试（0.5/1/2s）；4xx（含 401/403 鉴权失败）与 TLS 失败**立即放弃不重试**。
- **SSE 截断不崩**：流中途断开时，已收到的事件全部返回，末尾追加 `{"type":"error","message":"stream truncated after N events"}`，**不重连**（任务在远端状态已不确定）。
- **TLS**：首选公网域名 + Caddy ACME（`tls.verify=true`，走标准 CA 链）；备选自签证书 + 指纹固定（注册表 `tls.pinned_sha256`）。**禁止** `tls.verify=false` 且无指纹——注册表会直接 `PeerRegistryError` 拒绝。
- **密钥不落盘**：注册表只存环境变量名（`hmac_secret_ref`），密钥值只活在进程环境里。

---

## 密钥持久化与轮转

**重要局限**：`comm.add_peer` 把密钥写进的是 comm-agent **子进程的环境变量**（进程级）。orchestrator / comm-agent 一重启，注册表文件还在（`comm.list_peers` 照常列出远端），但密钥变量丢了——`comm.delegate` / `comm.chat` 会因 `resolve_secret` 取不到值而返回 `{"ok": false, "error": "env var ... not set"}`。

要跨重启保留，按返回的 `note` 自行持久化（本期不自动做）：

```bash
# Linux/macOS：写进 shell profile 或用 .env 加载器
export COMM_PEER_OPENCLAW_HOME_HMAC=<密钥>
```
```powershell
# Windows：设为用户级环境变量
[Environment]::SetEnvironmentVariable("COMM_PEER_OPENCLAW_HOME_HMAC", "<密钥>", "User")
```

**轮转**：HMAC 是对称密钥，轮转 = 改两边的值 + 重启。具体地：远端改 `a2a.env` 里的 `A2A_HMAC_SECRET` 并重启 OpenClaw / Caddy；主机端改对应的 `COMM_PEER_<ID>_HMAC` 并重启 comm-agent。**不做自动协商轮转。**

---

## 故障排查

| 现象 | 可能原因 / 处理 |
|---|---|
| 入站全部 401 | `COMM_AGENT_SELF_HMAC` 设成了密钥本身，而非"密钥变量名"。按[快速开始第 3 步](#3-配置入站密钥仅入站需要)设两个变量 |
| 启动日志 `could not start caddy ... stdio MCP only` | Caddy 不在 PATH。装 Caddy 或设 `CADDY_BINARY`；只做出站可忽略 |
| `comm.delegate` 返回 `unknown peer 'X'` | 没注册过该 peer，先 `comm.add_peer` |
| `env var '...' not set` | 密钥变量丢失（多半是重启后）。重新 export 或参考[密钥持久化](#密钥持久化与轮转) |
| `auth refused: HTTP 401/403` | 两边 HMAC 密钥不一致，或本机 `COMM_AGENT_MY_PEER_ID` 跟远端白名单 `your-peer-id` 不符 |
| `peer unreachable: ... (retried 3)` | DNS/TCP 连不上：检查远端 Caddy 是否在跑、`public-host`/端口是否放行 |
| `replay detected` | 同一 grant 被重发（每次调用应重新签）；正常使用不会触发 |
| `stream truncated after N events` | 远端中途断开。任务在远端状态不确定，按需重新委派 |
| TLS 证书错误 | 自签证书未做指纹固定：远端取 `openssl x509 -fingerprint -sha256` 填进注册表 `tls.pinned_sha256`，并以 `tls_verify=false` 注册 |
| 远端自测 `agent card not yet reachable` | Caddy 还没起或 ACME 还在签证书；稍等后手动 `curl -sk https://localhost:8443/.well-known/agent.json` |

---

## 当前限制与路线

本期（MVP）明确**不做**或**有限制**的部分：

- **入站委派是 stub**：被远端 `task.delegate` 呼叫时，本机目前返回 "not yet implemented"（`main.py::_noop_stream`）；`status/query` 返回固定 `idle`。出站委派（呼叫别人）功能完整。
- **斜杠命令开发中**：`/comm add|list|use|rm`、`/task`、`/chat` 让你绕过 planner 直接驱动 comm-agent，正在 `feat/comm-slash-commands` 分支实现（设计见 [`docs/superpowers/specs/2026-05-24-comm-agent-slash-commands-design.md`](../../docs/superpowers/specs/2026-05-24-comm-agent-slash-commands-design.md)）。当前只能经自然语言 → planner 触发 `comm.*`。
- **长任务受 30s 超时**：`A2AClient` 默认 30s 超时，长任务委派会失败；后台化留作后续。
- **任务不持久化**：A2A `task_id` 跑完即忘，不支持断线重连 / 状态恢复（`context_id` 多轮对话除外）。
- **不做**：mDNS / 自动发现、一对多广播、push notifications、TLS 叶证书指纹强制校验（v1.1）、并行委派多个 peer。

完整范围边界见设计稿 [`docs/superpowers/specs/2026-05-23-comm-agent-design.md`](../../docs/superpowers/specs/2026-05-23-comm-agent-design.md) §9。
