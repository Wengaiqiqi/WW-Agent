# W&W Agent

> 一个以终端为主要交互方式的多智能体系统，基于 LangChain 和 LangGraph 构建，支持飞书 / Lark、QQ 官方机器人等聊天平台网关，并为每个用户提供持久化记忆功能。
>
> 命令行优先的多 Agent 框架,自带飞书 / QQ 聊天网关 + 每用户持久化记忆。

[![Tests](https://img.shields.io/badge/tests-116%20passing-brightgreen)](#测试-tests)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](#依赖-dependencies)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

---

## ⚡ TL;DR

```bash
pip install -r requirements.txt
python cli.py                      # 多 Agent REPL(默认)
python cli.py --single             # 单 Agent 兼容模式
python -m gateway feishu           # 飞书机器人(长连接,无需公网)
python -m gateway qq               # QQ 官方机器人(沙箱 / 正式皆可)
```

---

## 目录 Table of Contents

- [项目定位](#项目定位)
- [系统架构](#系统架构-system-architecture)
- [跨进程协议](#跨进程协议-ipc-protocols)
- [工具架构](#工具架构-tool-architecture)
- [记忆架构](#记忆架构-memory-architecture)
- [聊天平台网关](#聊天平台网关-chat-platform-gateways)
- [Comm-agent 跨机通信](#comm-agent-跨机通信-cross-machine-a2a)
- [技能系统](#技能系统-skills)
- [安全模型](#安全模型-security-model)
- [技术栈](#技术栈-tech-stack)
- [测试](#测试-tests)
- [Roadmap](#roadmap)

---

## 项目定位

**一句话**:把"LLM + 工具 + 记忆 + IM 接入"拼成一个能命令行用、能挂群里、能多人共用的 Agent 产品。

跟单点 `chat.completions.create` 隔了好几层工程:

| 想要 | 朴素做法的问题 | 这个项目的方案 |
|---|---|---|
| 复杂多步推理 | 单轮提问失败成本高 | 多 Agent ReAct loop + 自动重试 |
| 工具调用安全 | 直接给 LLM `subprocess` 是洞 | 三层权限 gate + JWT 鉴权 |
| 大工具集 | 全堆给一个 LLM,上下文爆炸,路由错率高 | Orchestrator 路由 → 专家 Agent + Skills 按需 |
| 跨会话记忆 | 全历史 token 费指数级 | 25 轮滑窗 + 持久化关键事实(per-user) |
| 企业 IM 接入 | 飞书 / QQ 各一套协议、签名、限流、审核 | 统一 `gateway/` 薄适配层 |

---

## 系统架构 System Architecture

### 总览 Overview

```
              ┌─────────────────────────────────┐
   CLI ───→   │       Orchestrator              │  ←── /gateway feishu | qq
              │       (planner LLM,             │      (external IM events arrive here)
              │        permission gate,
              │        A2A routing)             │
              └────┬─────────────────────┬──────┘
                   │ MCP stdio +         │ MCP stdio +
                   │ A2A SSE             │ A2A SSE
                   ▼                     ▼
            ┌──────────────┐      ┌──────────────┐
            │  tool-agent  │      │  skill-agent │
            │  (ReAct loop)│      │  (SKILL.md   │
            │              │      │   executor)  │
            └──────┬───────┘      └──────┬───────┘
                   │                     │
                   ├─ read/write_file    ├─ baidu-ec-search
                   ├─ grep/glob_search   └─ ...
                   ├─ web_extract/crawl  
                   ├─ run_python/cmd
                   ├─ memory  (per-user)
                   └─ clarify
```

### 三个进程,职责分明

| 进程 | 职责 | 典型操作 |
|---|---|---|
| **orchestrator** | Planner LLM 决定每条请求路由到哪;签 JWT;流式合并子 Agent 输出 | "用户说要读文件" → `tool.task("read README.md")` |
| **tool-agent** | 工作区 + Web 专家;一个 ReAct loop 自主调工具直到拿到答案 | ReAct: 调 `read_file` → 看内容 → 调 `grep` → 答 |
| **skill-agent** | 跑 `skills/<slug>/SKILL.md` 里定义的领域工作流 | 跑"百度全网比价"那种带 API key 的封装流程 |

### 三种入口

```
┌─────────────────────────┐
│ python cli.py           │  ← 终端 REPL(默认多 Agent)
└─────────────────────────┘

┌─────────────────────────┐
│ python cli.py --single  │  ← 单 Agent 模式(legacy,快、低开销)
└─────────────────────────┘

┌─────────────────────────┐
│ python -m gateway xx    │  ← 聊天平台网关(独立进程)
└─────────────────────────┘

  REPL 内部还能 /gateway 子菜单
  ╰─→ 把网关作为 background task 跑在 REPL 同进程
```

---

## 跨进程协议 IPC Protocols

两种各管一段:

### MCP (Model Context Protocol) — 同步 RPC

**何时**:Orchestrator → Specialist 的**单次工具调用**。
- 一次性请求 / 响应
- 用 stdio JSON-RPC,Specialist 是 orchestrator fork 的子进程
- 适合 `read_file` / `calculator` / `current_datetime` 这种"一问一答"

```
orchestrator                                 tool-agent (subprocess)
    │                                              │
    │── tools/call name=read_file path=README ──→ │
    │                                              │── 真的去读 ──→ disk
    │                                              │
    │ ←── result: {text: "# ...", ...} ──         │
```

### A2A (Agent-to-Agent) — 流式 SSE

**何时**:Orchestrator → Specialist 的**任务级委托**(`tool.task` / `skill.<slug>`)。
- FastAPI + Server-Sent Events
- Specialist 内部跑一整个 ReAct loop / SKILL.md 流程
- 中间会吐 `thinking` / `tool_call` / `tool_result` / `text` / `done` 事件
- 也支持反向 `clarify_request`(子 Agent 主动问用户)

```
orchestrator                              tool-agent
    │                                          │
    │── POST /a2a/stream {task: "..."} ───→  │
    │                                          │  ── ReAct iter 1 ──→ LLM
    │ ←── event: thinking ─────────────       │
    │ ←── event: tool_call name=read_file ──  │  ── LLM 决定调工具 ──
    │ ←── event: tool_result ──────────────   │
    │ ←── event: thinking ─────────────       │
    │ ←── event: text chunk="阅读完毕..."     │  ← 流式吐 token
    │ ←── event: done text="..."  ─────────   │
```

### 跨进程鉴权 Cross-Process Auth

```
JWT (HS256, 60s TTL):
  ┌─────────────────────────────────────┐
  │ {                                   │
  │   "iss": "orchestrator",            │
  │   "aud": "tool-agent",              │
  │   "sub": "tool.task",               │
  │   "allowed_tools": ["read_file",    │
  │                     "grep_search"], │
  │   "permission_mode": "workspace-    │
  │                       write",       │
  │   "trace_id": "t9f3a1",             │
  │   "exp": 1716284321                 │
  │ }                                   │
  └─────────────────────────────────────┘
        ↓ 签
   HMAC key(orchestrator 启动时随机生成,只传给 spawn 的 specialist)
```

- 每次工具调用 mint 新 JWT,60 秒后过期
- `allowed_tools` 精确到工具名,**最小权限授予**
- 子 Agent `agents/shared/authz.verify_grant` 拒绝过期 / tamper / 不在 allow list 的请求
- HMAC key 每次 REPL 启动 re-keyed,长期泄露窗口为 0

---

## 工具架构 Tool Architecture

### 一份源码,两个表面 One Source, Two Surfaces

```
┌─────────────────────────────────────────────────────┐
│  tool/  ── 工具实现(单一真相)                       │
│  ├── tool_file_ops.py    (read/write/grep/glob)     │
│  ├── tool_shell.py        (run_python/run_command)  │
│  ├── tool_web.py          (search/extract/crawl)    │
│  ├── tool_memory.py       (memory)                  │
│  └── ...                                            │
└─────────────────────────────────────────────────────┘
        ↑                          ↑
        │                          │
   ┌────┴────────┐           ┌─────┴───────────┐
   │ tool/tools.py│           │ agents/tool_     │
   │ @tool 装饰器 │           │  agent/tool_      │
   │              │           │  executor.py     │
   │ ← legacy     │           │ _TOOL_MAP        │
   │   --single   │           │ ← multi-agent    │
   │   模式用     │           │   ReAct 用       │
   └─────────────┘           └─────────────────┘
```

修一个底层 bug,两个表面都好了。

### 工具清单 Tool Catalog

| 工具 | 权限要求 | 表面 | 干啥 |
|---|---|---|---|
| `read_file` | read | both | UTF-8 文件读 + 工作区边界检查 |
| `write_file` | write | both | UTF-8 文件写,自动建父目录 |
| `edit_file` | write | legacy | 精确字符串替换 |
| `apply_patch` | write | legacy | V4A 多文件原子 diff |
| `grep_search` | read | both | ripgrep 风格正则搜索 |
| `glob_search` | read | both | `**/*.py` 模式搜文件 |
| `list_directory` | read | both | 列目录 |
| `run_python` | inner | both | Python 子进程,180s 超时 |
| `run_command` | inner | both | shell 命令 |
| `web_search` | read | both | DuckDuckGo / Tavily |
| `web_extract` | read | both | 抓 URL → 可读文本(带 SSRF 防护) |
| `web_crawl` | read | both | 同 host BFS,最多 5 页 |
| `memory` | write | internal | 持久化关键事实,per-user 隔离 |
| `clarify` | read | internal | 反向问用户(SSE bridge) |
| `calculator` | read | legacy | AST 安全表达式求值 |
| `current_datetime` | read | legacy | 当前时间 |
| `osv_check` | read | legacy | OSV 包恶意 / CVE 查询 |
| `home_assistant` | danger | legacy | HA REST API |
| `x_search` | read | legacy | X(Twitter)搜索(xAI) |
| `vision_analyze` | read | legacy | 图像 + prompt → vision LLM |
| `mixture_of_agents` | read | legacy | MoA 论文实现 |

### 三层权限模型 Three-Tier Permission Model

三档:`read-only` / `workspace-write` / `danger-full-access`。**真正麻烦**的不是用户档,是 LLM 在 ReAct 里自己决定调啥——三层 gate 协同:

```
┌──────────────────────────────────────────────────────────┐
│  Outer Gate (orchestrator/permission_gate.py)            │
│  Planner 能直接 dispatch 的 capability 白名单              │
│  read-only:        read/grep/glob/web_*/calculator       │
│  workspace-write:  + write/edit/patch/memory             │
│  danger:           + run_command/run_python              │
└──────────────────────────────────────────────────────────┘
                              ↓ 通过
┌──────────────────────────────────────────────────────────┐
│  Inner Gate (tool_executor.tools_for_mode)               │
│  tool-agent ReAct loop 实际绑定的工具集                    │
│  - read-only: 不绑 write_/run_,模型根本不知道工具存在    │
│  - workspace-write: 绑 run_*(允许 pip install / 跑 .py) │
└──────────────────────────────────────────────────────────┘
                              ↓ 通过
┌──────────────────────────────────────────────────────────┐
│  Skill Gate (skills/<slug>/_meta.json::requiresTools)    │
│  每个 skill 显式声明能调哪些 tool-agent 工具                │
│  默认只读;`run_command` 等需写 requiresTools 列表          │
└──────────────────────────────────────────────────────────┘
                              ↓ 通过
                       JWT 校验 ──→ 执行
```

**关键设计**:不是"权限不足就拒绝"那种被动防御,而是**根本不告诉模型工具存在**。read-only 下 `make_langchain_tools` 直接不 bind write 类工具,模型 prompt 里根本没这工具的描述,模型也就不会主动调。

---

## 记忆架构 Memory Architecture

两层:**短期对话历史** + **长期持久事实**。

```
┌─────────────────────────────────────────────────────────┐
│  Per-chat 会话历史(短期)                                 │
│  位置: .langchain-agent/sessions/<sha256(chat_id)>.json │
│  大小: 25 轮,user + assistant 各 25 条                    │
│  机制: 滑动窗口,新对话进来挤掉最老的                       │
│  注入: planner 的 "Recent conversation" 上下文段          │
└─────────────────────────────────────────────────────────┘
                  +
┌─────────────────────────────────────────────────────────┐
│  Per-user 持久记忆(长期)                                 │
│  位置: .langchain-agent/memories/users/<sha256(uid)>/   │
│        ├── USER.md      (用户档案,4KB)                  │
│        └── MEMORY.md    (项目笔记,8KB)                  │
│  机制: LLM 主动调 `memory(action=add|replace|remove)`     │
│  注入: planner + tool-agent 的 system prompt(自动)       │
│        每条对话开始时 snapshot,这一回合期间不变            │
└─────────────────────────────────────────────────────────┘
```

### 多用户隔离

环境变量 `LANGCHAIN_AGENT_MEMORY_USER` 决定 `tool_memory` 读写哪个 user 的目录。Gateway 收到消息时,先 `os.environ[...] = sender_user_id` 再 spawn tool-agent → 子进程继承 env → tool_memory 路由到正确目录。

```
飞书群里两个用户聊同一个 bot:
  
  用户 A (open_id ou_aaa):
    "记住我叫张三"
        ↓
    gateway 设 LANGCHAIN_AGENT_MEMORY_USER=ou_aaa
        ↓
    tool-agent 调 memory(action=add, target=user, content="名字: 张三")
        ↓
    写到 memories/users/<sha256(ou_aaa)>/USER.md
  
  用户 B (open_id ou_bbb):
    "我叫什么?"
        ↓
    gateway 设 LANGCHAIN_AGENT_MEMORY_USER=ou_bbb
        ↓
    tool-agent 读 memories/users/<sha256(ou_bbb)>/USER.md  ← 空!
        ↓
    bot 回 "我不知道你叫什么"  ← 不会泄漏 A 的信息
```

### 安全约束

- 写入前 `_scan()` 拦截 prompt injection patterns(`ignore previous`、`disregard rules`、`curl ...$KEY` 等)
- 不可见 Unicode(零宽空格 / RTL 标记)直接拒
- 每个 target 有字节上限(USER 4KB / MEMORY 8KB),超出强制 replace/remove

---

## 聊天平台网关 Chat Platform Gateways

```
┌─────────────────────────────────────────────────────────┐
│  gateway/                                               │
│  ├── feishu_ws.py    飞书长连接(lark-oapi v1.x)         │
│  ├── feishu.py       飞书 webhook(production)          │
│  ├── _feishu_common.py  两个 adapter 共用的逻辑           │
│  ├── qq.py           QQ 官方机器人(ws gateway)         │
│  ├── manager.py      启停管理 + PID lock + 状态 query   │
│  ├── runner.py       消息 → orchestrator → 回复 的桥    │
│  ├── credentials.py  gateways.json 凭据存取             │
│  ├── session_store.py 25 轮对话历史                      │
│  ├── _pidlock.py     跨进程防双开                        │
│  └── __main__.py     `python -m gateway xx` 入口         │
└─────────────────────────────────────────────────────────┘
```

### 飞书 / Lark

| 模式 | 何时用 | 配置 |
|---|---|---|
| **长连接(默认)** | 本地开发,无公网 | `app_id` + `app_secret`(SDK 内部协商) |
| **Webhook** | 生产部署,有 HTTPS | + `verify_token` + 可选 `encrypt_key` |

```
飞书云                  本机 gateway
   │                       │
   ws msg ─────────────→   _on_message
                              │
                              ├── 加 reaction("Typing")  ← 用户立刻看到"已收到"
                              ├── dedup(msg_id, 24h TTL)
                              ├── 群?要 @bot 才理(检查 mention)
                              ├── 工作线程 asyncio.run(run_turn(text))
                              │      │
                              │      ↓
                              │   orchestrator 一整轮 → 回复
                              │
                              ├── reply_message API
                              └── 删 reaction
```

### QQ 官方机器人

走**WebSocket Gateway**(同 hermes-agent),不走 Webhook(无需公网)。Intents 默认 `(1<<25) | (1<<30)` = C2C + 群@ + 频道@。

```
QQ open platform           本机 gateway
   │                            │
   POST /app/getAppAccessToken  │
   ◄──── 回 access_token ──     │
                                │
   GET /gateway                 │
   ◄──── 回 wss URL ────         │
                                │
   wss handshake (op:10 Hello)  │
   op:2 Identify ──────────────►│
   ◄── op:0 READY ─────────────│
                                │
   op:0 C2C_MESSAGE_CREATE      │
        / GROUP_AT_MESSAGE      │
        / AT_MESSAGE  ─────────►│
                                │── 路由表:
                                │   GROUP_AT  → /v2/groups/{id}/messages
                                │   C2C       → /v2/users/{id}/messages
                                │   AT_MSG    → /channels/{id}/messages
                                │
                                │── 出站 POST 用 sync httpx + threading
                                │   (Windows + ws + asyncio 共存的坑)
                                ▼
   POST .../messages ─────────  ws 回复给用户
```

### 网关之间的设计差异

| | Feishu | QQ |
|---|---|---|
| **入站协议** | lark-oapi v1 ws | 自定义 ws + op codes |
| **出站协议** | lark-oapi sync client | 同步 httpx 走 v2 REST |
| **签名** | SDK 内部 token | `Authorization: QQBot <token>` |
| **沙箱** | 自建应用直接可用 | 必须先过 QQ 资质审核 |
| **反馈** | `message.reaction` ("Typing") | 无公开 reaction API |
| **跑在哪** | 工作线程 + lark SDK 自带 loop | 工作线程 + 自建 isolated loop |
| **干净停** | SDK 无 stop API,只能进程退 | `threading.Event` 协作式取消 |

---

## Comm-agent 跨机通信 (cross-machine A2A)

The `comm-agent` specialist speaks Google A2A v0.3 over HTTPS so your
main REPL can delegate tasks to or chat with agents running on other
machines (e.g. an OpenClaw or Hermes instance).

**Tools exposed:** `comm.list_peers`, `comm.add_peer`, `comm.remove_peer`,
`comm.peer_card`, `comm.delegate`, `comm.chat`, `comm.status`.

**Quick start (host side):**

1. Install Caddy (used for TLS): https://caddyserver.com/docs/install
2. Set the inbound HMAC secret env var:
   ```bash
   export COMM_AGENT_SELF_HMAC=$(openssl rand -hex 32)
   ```
3. Start the REPL — the comm-agent specialist auto-spawns when present in
   `.agent/agents/`.

**Connecting a remote OpenClaw (the example case):**

On the remote machine, run our install script:

```bash
curl -sSL https://raw.githubusercontent.com/<repo>/main/scripts/install_openclaw_a2a.sh \
  | bash -s -- \
      --my-peer-id openclaw-home \
      --your-peer-id agent-last-laptop \
      --public-host home.example.com \
      --hmac-secret "$(openssl rand -hex 32)"
```

The script prints the HMAC secret once. Back in the host REPL, register
the remote:

```
comm.add_peer peer_id=openclaw-home url=https://home.example.com:8443 hmac_secret_value=<the-secret>
```

After that, the orchestrator can delegate via `comm.delegate peer_id=openclaw-home task="..."`.

**Connecting a remote Hermes:** Hermes speaks stdio ACP (not A2A), so it needs
the A2A↔ACP bridge — run `scripts/install_hermes_a2a.sh` (or `.ps1`) on the
Hermes host, then `comm.add_peer` exactly as above. See
`agents/comm_agent/README.md` → "对接 Hermes（A2A↔ACP 桥接）".

**Security model:**

- Every cross-machine call carries an HMAC-SHA256 grant scoped to
  `(my_peer_id, target_peer_id, requested_skill, nonce, 60s exp)`. Replay
  is blocked by a 10k-entry LRU on the verifier.
- TLS is handled by Caddy (ACME by default; self-signed for LAN/VPN).
- The peer registry stores only env-var **names**; the secret value lives
  in process env only. Persist via your shell profile or a `.env` loader.

See `docs/superpowers/specs/2026-05-23-comm-agent-design.md` for the full design.

---

## 技能系统 Skills

`skills/<slug>/SKILL.md` 定义一个领域工作流;`_meta.json` 声明权限和环境变量。

```
skills/baidu-ecommerce-search/
├── SKILL.md         系统提示 + 工作流描述
├── _meta.json       slug / matchKeywords / requiresEnv / requiresTools
└── scripts/         Python 工具脚本(skill-agent 通过 run_command 调)
```

`_meta.json` 示例:
```json
{
  "slug": "baidu-ecommerce-search",
  "matchKeywords": ["比价", "京东", "全网", "拼多多"],
  "requiresEnv": ["BAIDU_EC_SEARCH_TOKEN", "BAIDU_EC_SEARCH_QPS"],
  "requiresTools": ["run_command", "read_file"]
}
```

- `matchKeywords` 驱动单 agent 模式自动注入;多 agent 模式由 planner 看 description 决定。
- `requiresEnv` 是 secret filter 的白名单——默认 subprocess 看不到 `*KEY/*TOKEN/*SECRET`,但 skill 显式声明的就放行。
- `requiresTools` 是 skill 能调 tool-agent 哪些工具(最小权限,默认只读)。

---

## 安全模型 Security Model

| 风险 | 防御 |
|---|---|
| **SSRF**(web_extract / vision_analyze) | 自定义 `urllib` opener + `SafeRedirectHandler`,DNS 解析后逐 IP 检查私有/loopback/multicast,30x 重新校验 |
| **subprocess 泄密** | 默认从 env 删 `KEY/TOKEN/SECRET/HMAC/AUTH/API` 相关。skill 用 `_meta.json::requiresEnv` 显式 opt-in |
| **跨工作区** | `tool/tool_file_ops.resolve_workspace_path` resolve 后跟 `LANGCHAIN_AGENT_WORKSPACE_ROOT` 比对 |
| **Calculator 注入** | AST 安全求值,不用 `eval` |
| **HA call_service** | 拒 `shell_command` / `python_script` / `command_line` / `rest_command` / `pyscript` / `hassio` 域 |
| **Memory 注入** | 写入前过 threat patterns + 不可见 unicode 检测 |
| **重复发送** | 飞书 / QQ 各自 dedup(message_id LRU,24h / 5min TTL) |
| **跨平台多开** | `.langchain-agent/<platform>.pid` PID lock,stale 自动接管 |

---

## 技术栈 Tech Stack

```
应用层
├── LangChain 0.3 / LangGraph 0.2     ── 编排
├── Rich + prompt_toolkit              ── TUI

协议层
├── mcp >= 1.0                         ── 跨进程同步 RPC
├── a2a-sdk >= 0.2                     ── 任务级 SSE 流式委托
└── pyjwt                              ── 跨进程鉴权

平台层
├── FastAPI / uvicorn                  ── 飞书 webhook + A2A server
├── httpx                              ── 同步 + 异步 HTTP
├── websockets >= 12                   ── QQ 官方机器人 ws
├── lark-oapi >= 1.4                   ── 飞书 SDK + 长连接
└── cryptography                       ── 飞书 encrypt mode AES-256-CBC

测试
└── pytest + pytest-cov                ── 116 个 gateway 单测
```

模型 provider 都在 `config/_providers.py`:OpenAI、Anthropic、DeepSeek、Gemini、xAI、Moonshot、阿里、腾讯 TokenHub、OpenRouter、AI Gateway 等 20+ 个,统一抽象为 OpenAI-chat / Anthropic 两种协议。

---

## 测试 Tests

```bash
pytest                              # 全套 (~45 个测试文件)
pytest tests/test_gateway/          # 只跑 gateway 模块 (116 个用例 ~ 1.5s)
pytest -k "not e2e"                 # 跳过 subprocess-spawn 的 e2e 测试
pytest --cov=gateway --cov-report=term-missing
```

Gateway 模块的覆盖率:

| 模块 | 覆盖率 |
|---|---:|
| `gateway/_feishu_common.py` | 98% |
| `gateway/session_store.py` | 94% |
| `gateway/credentials.py` | 94% |
| `tool/tool_memory.py` | 86% |
| `gateway/_pidlock.py` | 84% |
| 网络 / SDK 路径(`feishu_ws.py` / `qq.py` 网络部分) | 手动测试 |

---

## Roadmap

按优先级:

- [ ] **集成测试**:mock httpx + uvicorn + websockets,覆盖 manager 启停状态机
- [ ] **Metrics**:Prometheus 端点 / ndjson 滚动统计(QPS / 延迟 / token 用量)
- [ ] **更多平台**:DingTalk / 企业微信 / Discord(照 `feishu_ws.py` 模板)
- [ ] **常驻 specialist**:每个 gateway 进程内 spawn 一次,不再 per-turn 重启(省 2-3s 启动延迟)
- [ ] **per-user 并发**:目前 `_CONCURRENCY_GUARD` 全局串行,改成 per-user 桶可以扛多人并发
- [ ] **`/reset` slash 命令**:bot 收到这条直接清当前 chat 的会话历史
- [ ] **lark-oapi SDK 优雅 stop**:目前飞书 ws 线程没法干净退,等上游或自实现

---

## 致谢 Acknowledgements

- **hermes-agent**:Gateway 的整体架构思路(平台 adapter / session / memory) — 我做了 ~95% 的功能裁剪,留下"够用的 agent + IM 桥"那部分
- **LangChain / LangGraph**:核心编排框架
- **lark-oapi-sdk-python**:飞书长连接 SDK
- **MCP / A2A SDK**:跨进程协议标准

---

## 项目结构 Project Structure

```
agent/
├── cli.py                       入口(--single 或多 agent)
├── agent_paths.py               配置目录解析
├── project_context.py           发现 agent.md / instruction 文件
├── prompt_rules.py              共享 prompt 风格规则
│
├── config/                      Provider 注册 + 凭据 + LLM factory
├── orchestrator/                多 Agent 主进程
│   ├── main.py                  run_repl / run_prompt 入口
│   ├── repl_controller.py       turn 执行 + A2A 流式
│   ├── repl_commands.py         slash 命令(/gateway 等)
│   ├── turns.py                 LLMPlanner + TurnRunner
│   ├── graph.py                 LangGraph plan → dispatch
│   ├── mcp_host.py              spawn + JWT-gated MCP 会话
│   ├── a2a_client.py            出站 SSE 流式委托
│   ├── permission_gate.py       外层 authz(签 JWT)
│   ├── router.py                CapabilityRouter
│   ├── picker.py                共享箭头键选单组件
│   └── ...
│
├── agents/                      Specialist 子进程
│   ├── shared/                  MCP/A2A 框架、JWT 校验、权限模式
│   ├── tool_agent/              ReAct 工作区 + Web 专家
│   └── skill_agent/             SKILL.md JSON-envelope 执行器
│
├── tool/                        工具实现(单一真相)
├── skills/                      bundled skills
├── gateway/                     聊天平台网关
│   ├── feishu_ws.py             飞书长连接
│   ├── feishu.py                飞书 webhook
│   ├── qq.py                    QQ 官方机器人
│   ├── manager.py               启停 + PID lock
│   ├── runner.py                消息 → orchestrator 桥
│   ├── session_store.py         25 轮对话历史
│   ├── credentials.py           gateways.json
│   ├── _feishu_common.py        飞书两 adapter 共享代码
│   ├── _pidlock.py              防双开
│   └── __main__.py              `python -m gateway xx`
│
└── tests/                       45 个测试文件
    └── test_gateway/            116 个 gateway 单测
```

---

## License

MIT
