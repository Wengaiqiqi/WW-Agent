# Web UI — 设计稿

> 日期: 2026-05-27
> 状态: 已批准设计,待写实现计划

## 1. 目标与范围

为这个 CLI-first 的多智能体系统增加**第三个交互表面**:一个公网可部署、多用户的
Web 聊天界面。继终端 REPL、IM 网关之后,用户通过浏览器登录后即可与 agent 对话。

**明确的使用场景**(已与用户确认):

- 公网部署,多用户。
- 用户名 + 口令自建账号(口令哈希存储,签发 JWT)。
- 浏览器里**流式逐字输出**,可展开查看 thinking / 工具调用过程。
- 多会话管理(ChatGPT 式侧栏)、Markdown 渲染、用户可在已配置的 provider/model 间选择。
- 权限档为 **`workspace-write`**(服务端每轮强制,用户不可改):可读写 / 创建文件、跑
  Python / shell。**注意**:该档含 `run_python` / `run_command`,即公网用户能在服务器上
  执行代码 —— 风险与缓解见 §3。
- 前端用**原生 HTML/CSS/JS,由 FastAPI 托管**,不引入 Node 构建链。

**只服务多智能体系统**:Web 层复用 orchestrator 核心(planner → A2A/MCP dispatch),
**不接 `--single` 兼容模式**(`tool/tools.py` + `@tool` 那条独立 legacy 路径)。

**不在本次范围**:`--single` 兼容模式、operator 级管理动作(`/task` `/chat` `/peers`、
网关启停、peer 注册)、持久 specialist / per-user 并发优化(见 §9 已知限制)、
第三方 OAuth 登录。

## 2. 总体架构

新增一个与 `gateway/` 平级的 `web/` 包。它**不重写编排逻辑**——复用
`gateway.runner` 那套「bootstrap orchestrator → planner → dispatch」的核心,只把
「丢弃中间流、只返回最终文本」改成**把事件流式转发给浏览器**。

```
浏览器 (SPA)  ──HTTPS──►  web/ FastAPI app  ──复用──►  orchestrator 核心
  │                          │                          (planner + A2A/MCP dispatch)
  │  ① 登录/注册              ├─ auth   (JWT + pbkdf2)
  │  ② 会话 CRUD              ├─ store  (SQLite: users/conversations/messages)
  │  ③ 发消息(SSE 流式)      ├─ bridge (run_turn_streaming → 事件流)
  │  ◄── thinking/tool/text ──┤  ↑ 每轮强制 workspace-write 档 + 每用户工作区 + 选定模型
  └───────────────────────────┘
```

部署:`python -m web`(对齐 `python -m gateway`),起一个 uvicorn 进程。
**TLS 由前置反代终结**(Caddy / nginx)——仓库已用 Caddy 给 comm-agent 做 TLS,沿用同一套。

## 3. 安全模型(公网多用户的重点)

**前提**:Web 用户跑在 `workspace-write` 档,该档含 `run_python` / `run_command`
(`permission_modes.py:65-69`)。意味着**任何拿到账号的用户都能在服务器上执行代码**
(RCE)。下面的防御不是消除这一点,而是限制「谁能拿到账号」和「能伤到什么」。

| 风险 | 防御 |
|---|---|
| **任意命令/代码执行(RCE)** | 这是 `workspace-write` 的固有能力,无法在保留「写文件 / 跑脚本」的同时去掉。缓解:① **强烈建议整个 `python -m web` 跑在隔离容器 / VM 里**,环境内不放任何密钥或有价值数据(用户能 `run_command` 读 env);② 注册闸门(下行)限制谁能拿到账号。**这两条作为部署强制约束写进文档(§11)。** |
| **偷服务端密钥** | 子进程默认从 env 删 `KEY/TOKEN/SECRET/HMAC/AUTH/API` 相关(复用现有 secret filter)。但 `run_command` 仍可探测文件系统 —— 故依赖上面的容器隔离作为兜底。 |
| **写坏 / 读到他人文件、改到项目源码** | **每用户独立工作区**:每轮在锁内把 `LANGCHAIN_AGENT_WORKSPACE_ROOT` 设为 `.langchain-agent/web/workspaces/<web_user_id>/`。文件读写边界(`resolve_workspace_path`)随之限定在该用户私有目录,用户之间互不可见,也碰不到项目源码。 |
| **跨用户串数据** | 每轮设 `LANGCHAIN_AGENT_MEMORY_USER=<web_user_id>`(复用现有 per-user memory 隔离);会话归属在 SQLite 查询层校验 `conversation.user_id == current_user`。 |
| **谁能注册** | `WEB_SIGNUP_CODE` 注册码闸门(留空=开放注册)。**公网 + workspace-write 下强烈建议必填**,否则等于把 RCE 开放给任何人。部署文档默认开启。 |
| **凭据泄露** | LLM API key 全在服务端 config;用户只能在「已配置凭据的 provider」里选,看不到 key。 |
| **口令安全** | `hashlib.pbkdf2_hmac`(SHA-256, ≥200k 迭代)+ 每用户随机 salt(stdlib,不引新依赖);会话用 JWT(HS256,复用已有 `pyjwt`),放 **httpOnly + SameSite=Strict + Secure** cookie——SameSite=Strict 即 CSRF 防线(同源 SPA)。 |
| **滥用 / 刷量** | 每用户令牌桶限流(默认 20 轮 / 分,可配)+ 单条消息长度上限(默认 8KB)+ 注册 signup code 闸门(`WEB_SIGNUP_CODE`,留空=开放注册;公网部署文档建议开启)。 |
| **slash 命令越权** | Web 表面**不路由** `/task` `/chat` `/peers` 等(operator 驱动远程 peer 的高权限动作),一切都当普通聊天走 planner。 |

服务端密钥:JWT 签名密钥取自 `WEB_AUTH_SECRET` 环境变量;未设置时启动报错(生产),
或开发模式下自动生成临时密钥并打 warning。

## 4. 组件分解

```
web/
├── __main__.py        python -m web 入口,起 uvicorn(host/port 可由 env 配)
├── app.py             FastAPI app 工厂:挂路由 + 静态文件
├── auth.py            注册/登录、pbkdf2 口令、JWT 签发/校验、current_user 依赖
├── store.py           SQLite 存取:users / conversations / messages
├── bridge.py          run_turn_streaming(prompt, ...) -> AsyncIterator[event]
│                       复用 runner 的 bootstrap + dispatch,强制权限档 + 注入选定模型
├── ratelimit.py       per-user 令牌桶
├── models.py          列出「已配置凭据」的 provider / model(读 config.PROVIDERS)
└── static/
    ├── index.html     单页:登录态 + 会话侧栏 + 聊天区
    ├── app.js         会话管理、SSE 消费、Markdown 渲染、模型选择
    └── styles.css
tests/test_web/        单测(auth / store / bridge / 限流 / 路由)
```

模块边界清晰、可独立测试:`store` 不碰 HTTP;`auth` 不碰编排;`bridge` 不碰 SQLite
(只产出事件,路由层负责落库)。

## 5. 数据存储(SQLite,stdlib `sqlite3`,无新依赖)

位置 `.langchain-agent/web/app.db`,贴合「`.langchain-agent/` 下放运行态」的现有约定。

```sql
users(id TEXT PK, username TEXT UNIQUE, pwd_hash TEXT, salt TEXT,
      role TEXT DEFAULT 'user', created_at INT)

conversations(id TEXT PK, user_id TEXT, title TEXT,
              created_at INT, updated_at INT)

messages(id TEXT PK, conversation_id TEXT, role TEXT,    -- user | assistant
         content TEXT, events_json TEXT,                 -- 展开的 thinking / tool 过程
         created_at INT)
```

planner 的「最近对话」上下文仍复用现有 `gateway.session_store`(传
`session_key = conversation_id`);SQLite 这份是 UI 展示 + 会话列表的持久真相,各司其职。

## 6. 一轮聊天的数据流(流式)

1. 浏览器 `POST /api/conversations/{id}/messages`,body `{content, model}`,服务端以
   `text/event-stream` 响应(前端用 `fetch` + `ReadableStream` 读,不用 `EventSource`——
   它只支持 GET)。
2. 服务端:JWT 校验 → 限流 → 校验会话归属 → **进全局并发锁**,在锁内设置本轮 env
   (`LANGCHAIN_AGENT_MEMORY_USER`、强制 `LANGCHAIN_AGENT_PERMISSION_MODE=workspace-write`、
   每用户 `LANGCHAIN_AGENT_WORKSPACE_ROOT`、选定模型)→ 调 `bridge.run_turn_streaming`。
   锁内 set / finally 恢复,贴合 `runner._run_turn_locked` 现有的 env 存取还原手法。
3. `bridge` 三分支(对齐 `runner._dispatch_decision`):
   - **A2A 委托**(`tool.task` / `skill.*`):转发 `thinking` / `tool_call` /
     `tool_result` / `text` / `done` 事件;
   - **planner 散文** / **简单 MCP 调用**:无中间流,整段作为一个 `text` + `done` 发出。
4. 每个事件按 SSE 格式写回浏览器。`done` 后把 user + assistant 两条(含展开过程的
   `events_json`)落 SQLite,并 append 到 `session_store`。
5. 前端:`text` delta 累加进气泡(`done` 时整体重渲染 Markdown);`thinking` /
   `tool_*` 进可折叠的「过程」区。

错误处理:`bridge` 把任何异常转成一个 `error` 事件后再 `done`,前端在气泡里显示
`[error] ...`;HTTP 层异常(401/403/429)在 SSE 建立前以普通 JSON 返回。

## 7. API 端点

```
POST   /api/auth/register     {username, password, signup_code?}
POST   /api/auth/login        {username, password}        → set-cookie JWT
POST   /api/auth/logout
GET    /api/me                                             → 当前用户

GET    /api/models                                         → 可选 provider/model 列表
GET    /api/conversations                                  → 我的会话列表
POST   /api/conversations     {title?}                     → 新建
PATCH  /api/conversations/{id} {title}                     → 重命名
DELETE /api/conversations/{id}                             → 删除
GET    /api/conversations/{id}/messages                    → 历史消息
POST   /api/conversations/{id}/messages  {content, model}  → SSE 流式回复
```

所有 `/api/*`(除 register/login)需有效 JWT;会话相关端点额外校验归属。

## 8. 前端(原生,无构建)

单页 `index.html`:未登录显示登录 / 注册卡片;登录后左侧会话栏(新建 / 切换 /
重命名 / 删除)、右侧聊天区 + 顶部模型下拉。Markdown 用 CDN 的 `marked.js`,
代码高亮 `highlight.js` + 一键复制。SSE 事件驱动气泡增量更新;过程区默认折叠。

## 9. 测试

`tests/test_web/`,对齐现有 pytest 风格:

- `auth`:pbkdf2 口令哈希 / 校验、JWT 签发 / 校验往返、过期与篡改拒绝。
- `store`:users / conversations / messages CRUD,跨用户归属隔离(用户 A 取不到
  用户 B 的会话)。
- `ratelimit`:令牌桶超限拒绝、恢复。
- `auth gate`:注册 signup code 闸门、未登录访问 `/api/*` 拒绝、跨用户访问他人会话拒绝。
- `bridge`:用**注入的假事件流**(对齐 `delegate_via_a2a` 的 `delegate` 注入手法)
  验证三分支事件转发与落库,不起真子进程;并断言每轮在锁内正确设置了
  `permission_mode=workspace-write`、per-user `WORKSPACE_ROOT` 与 `MEMORY_USER`,
  且 finally 后还原 env(两个不同用户的 turn 不串工作区)。

## 10. 已知限制(诚实标注)

- **吞吐**:复用 `gateway.runner._CONCURRENCY_GUARD` 全局串行 + 每轮 spawn specialist
  (2–3s 启动),公网多人会排队。v1 取**正确性优先**;后续可接 Roadmap 的「常驻
  specialist + per-user 并发桶」提速。本次不扩大改这块(风险高、超范围)。
- **`workspace-write` 含 `run_python` / `run_command`,即公网用户可在服务器执行代码** ——
  本设计用「每用户独立工作区 + 注册码闸门 + 强制容器隔离部署」缓解,但**无法消除**。
  若运营方无法接受该残余风险,应改用 read-only 档(本次按用户明确要求采用 workspace-write)。

## 11. 部署须知(写进 README / 文档)

- **必须**把整个 `python -m web` 跑在隔离容器 / VM 里,环境内不放任何密钥或有价值数据 ——
  `workspace-write` 含 `run_command`,用户能读 env 与文件系统(见 §3 / §10)。
- **必须**设 `WEB_AUTH_SECRET`(JWT 签名密钥,随机且保密),未设拒绝启动。
- 公网**强烈建议**设 `WEB_SIGNUP_CODE` 限制注册;不设=把代码执行能力开放给任何注册者。
- 用户工作区由服务端自动管理在 `.langchain-agent/web/workspaces/<user_id>/`,operator 无需
  手动设 `LANGCHAIN_AGENT_WORKSPACE_ROOT`(每轮被覆盖)。
- 前置反代(Caddy / nginx)终结 TLS 并转发到 `python -m web` 的本地端口。
- 确保至少一个 provider 的 API key 已在服务端 config 配好,否则模型下拉为空。
