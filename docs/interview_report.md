# W&W Agent — 技术报告

> 一个基于 LangChain + LangGraph 的多 Agent 终端助手 + 多平台聊天网关。
>
> 面向面试场景的项目介绍。可整篇讲也可按章节抽。

---

## 一、项目一句话

**一个把 LLM 编排能力包装成"既能命令行用、也能挂到飞书/QQ 群里跟人对话"的 multi-agent 系统**——内置工具调用、技能编排、跨会话记忆、权限隔离,核心代码 ~1.5 万行 Python。

---

## 二、解决什么问题

LLM 单点调用(`chat.completions.create`)和"能干活的智能体"之间隔了好几层工程问题:

| 问题 | 朴素做法的缺陷 |
|---|---|
| 复杂任务需要多步推理 | 单轮提问无法自我修正,失败成本高 |
| 工具调用需要鉴权 / 沙箱 | 直接给 LLM 跑 `subprocess` 是安全洞 |
| 多种工具能力都堆给一个模型 | 上下文爆炸,planner 路由错误率高 |
| 跨会话要"记住事" | 每次都喂全历史,token 费指数增长 |
| 想接入企业 IM | 飞书/QQ 各有自己一套协议、签名、限流、审核 |

这个项目把这些问题各自分层解决,**最后形成一个能从 CLI 跑、也能放到飞书群里跟多人对话的完整 agent 产品**。

---

## 三、整体架构

### 3.1 两种运行形态共用一份核心

```
                  ┌──────────────────┐
   CLI / REPL ──→ │   Orchestrator   │ ←── /gateway feishu / qq
                  │  (planner LLM)   │
                  └──────────────────┘
                       │       │
              ┌────────┘       └────────┐
              ▼                         ▼
     ┌─────────────┐           ┌─────────────┐
     │ tool-agent  │           │ skill-agent │
     │  (ReAct)    │           │ (SKILL.md)  │
     └─────────────┘           └─────────────┘
       MCP stdio                  MCP stdio
       + A2A SSE                  + A2A SSE
```

- **orchestrator**:Planner LLM 决定每条请求路由到哪。三种分支:直接散文回答、`tool.task` 委托给 tool-agent、`skill.<slug>` 委托给 skill-agent。
- **tool-agent**:工作区 + Web 专家。一个 ReAct loop,绑定 `read_file` / `write_file` / `run_command` / `web_extract` / `memory` 等工具。
- **skill-agent**:跑领域工作流(`skills/<slug>/SKILL.md`),JSON envelope 协议反过来调 tool-agent 的工具。

### 3.2 进程间协议:**MCP stdio + A2A SSE**

- **MCP**(Model Context Protocol):Orchestrator → 子进程的同步调用,用 stdio JSON-RPC。注册一次,后面所有工具列表 / schema 都从这条路走。
- **A2A**(Agent-to-Agent):任务级流式委托,FastAPI + SSE。`tool.task` 这种"长跑任务"走 A2A,可以中途流式吐 token、`tool_call` / `tool_result` 事件、`clarify_request`(让 bot 反过来问用户)。

### 3.3 子进程生命周期

每个会话:
1. orchestrator 启动时读 `.agent/agents/*.card.json`(Agent Card),`spawn` 出 tool-agent / skill-agent
2. 双方握手 JWT(HS256, 60s TTL, 每次 orchestrator 启动重新 mint)
3. 子进程的 A2A URL 写入 `.agent/runtime/<id>.a2a-url`,orchestrator 读到后做路由
4. REPL 退出时 `host.shutdown_all()` 干净结束

---

## 四、技术亮点

### 4.1 三层权限模型 —— 防止"模型自己升权"

权限分三档:`read-only` / `workspace-write` / `danger-full-access`。

但**真正麻烦的是**:用户选的是"外层"权限,模型在 ReAct loop 里**自己决定**调哪个工具——如果模型在 `read-only` 下决定调 `run_command`,谁来拦?

解法是**三层 gate**:

| Gate | 在哪 | 拦什么 |
|---|---|---|
| Outer | orchestrator `PermissionGate` | Planner 能直接 dispatch 哪些 capability |
| Inner | tool-agent `tools_for_mode()` | ReAct loop 能 bind 哪些工具——**没 bind 就告诉模型这工具不存在**,从源头杜绝 |
| Skill | `_meta.json::requiresTools` | 每个 skill 单独声明能调哪些 tool-agent 工具,最小权限 |

跨进程鉴权用 **JWT 短期凭证**:每次工具调用,orchestrator mint 一个 60s 的 token,`allowed_tools` 字段精确到工具名;tool-agent 端 `verify_grant` 校验过期、tamper、tool 是否在 allow list。被吊销 / 越权直接拒。

### 4.2 工具实现:**一份源码,两个表面**

`tool/tools.py` 是 LangChain `@tool` 装饰器表面(legacy 单 agent 模式直接用);`agents/tool_agent/tool_executor.py` 是 `_TOOL_MAP` 表面(multi-agent 模式 ReAct loop 用)。

**两层都 import 同一个底层实现**(`tool/tool_file_ops.py`、`tool/tool_shell.py` 等等),意味着:

- 修一个 bug,两个表面都好了
- 文档只写一处
- 测试覆盖一遍管两个

### 4.3 安全细节

| 风险 | 防御 |
|---|---|
| SSRF(`web_extract` / `web_crawl`) | 自定义 `urllib` opener + `SafeRedirectHandler`,DNS 解析后逐 IP 检查是否私有/loopback/multicast,redirect 也重新校验 |
| Subprocess 泄密 | `run_command` / `run_python` 启子进程时,默认从 `os.environ` 删除匹配 `KEY/TOKEN/SECRET/HMAC/AUTH/API/...` 的变量。Skill 想要某个特定 env 时,在 `_meta.json::requiresEnv` 显式声明 |
| 跨工作区路径 | `resolve_workspace_path` 把所有路径 resolve 到绝对,跟 `LANGCHAIN_AGENT_WORKSPACE_ROOT` 比对,跨出去直接拒 |
| Memory 注入 | `memory` 工具写入前过一遍 threat patterns(`ignore previous`、`disregard rules`、`curl...$KEY` 等),阻止"被记忆诱导改 system prompt" |

### 4.4 V4A 补丁格式

多文件批量编辑用 `apply_patch`,自定义 `*** Begin Patch / Update File / Add File / Delete File / Move File / End Patch` 语法。**关键设计**:**所有 hunk 都通过校验后才落盘**——不会中途失败留半改不全的工作区。

### 4.5 Multi-agent 编排:LangGraph + 自研 router

orchestrator 内部用 LangGraph 串成 `plan → dispatch → END` 状态机,planner 节点返回 `{"capability": "...", "arguments": {...}}` 后 router 决定走哪个子 agent。

- **CapabilityRouter**:`router.register(agent_id, ["tool.task"], priority=10)`,同名 capability 按 priority 排序,可以做能力升级 / 灰度
- **StreamMux**:三路输出(planner 流、tool-agent 流、skill-agent 流)合到一个 TUI 上,每行根据来源加 `[orchestrator]` / `[tool]` / `[skill]` 前缀,**只有行首打 tag,中间不重打**——长 token 流不会被切碎

---

## 五、最近的重点工作:多平台聊天网关

### 5.1 动机

CLI 只能开发者自己用。要让 agent 在公司里真正被用起来,得接入**工作中已经在用的 IM**——飞书、QQ。

参考了 [hermes-agent](https://github.com/hermes/) 的 gateway 架构(那套 1.2 万行,自带 session/mirror/pairing/identity 整套体系),但**裁剪到最小可用版**——核心只做"消息进来 → 调 orchestrator → 把回复发回去"。

### 5.2 飞书 WS 长连接 + QQ 官方机器人

| | 飞书 | QQ |
|---|---|---|
| 协议 | lark-oapi v1 长连接 ws | 官方机器人 ws gateway v2 |
| 入站 | `im.message.receive_v1` 事件 | `C2C_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE` / `AT_MESSAGE_CREATE` |
| 出站 | `im/v1/messages/{id}/reply` | `/v2/users/{openid}/messages`、`/v2/groups/{openid}/messages`、`/channels/{id}/messages` |
| 鉴权 | tenant_access_token | QQBot access_token + ed25519 (跳过——用 ws 不需要) |
| 反馈 | `message_reaction.create` 加 "Typing" badge | 没有公开 reaction API |

**统一抽象**:`gateway/_feishu_common.py` 把 webhook 和 ws 两个 adapter 的共享逻辑(配置规整、文本抽取、提及剥除、bot 识别、截断)抽出来,两个 adapter 只保留传输层差异。

### 5.3 踩过的坑(面试可以重点讲)

#### 坑 1:Windows ProactorEventLoop + 长 WS + httpx 出站 = 永久 hang

QQ adapter 跑起来,WS 连上、收事件、调 LLM 都正常,**就是出站 POST 永远不返回**。

排查:
1. 写了独立 probe 脚本(短命进程 + fresh httpx client)直接打同一个端点 → 0.4 秒成功
2. 加 `asyncio.wait_for(20s)` 兜底 → **超时都不触发**
3. 换 `httpx.Limits(max_keepalive_connections=0)` → 没用
4. 换 `WindowsSelectorEventLoopPolicy` → 还是 hang

最后定位是:**`websockets` 的 SDK 长期占用了 asyncio 的 I/O 调度**,httpx 异步路径在 Windows 这种特定组合下被饿死。

**最终修法**:把 QQ 的所有 HTTP 调用换成**同步 httpx + `asyncio.to_thread`**——彻底绕开异步 I/O 调度。`asyncio.wait_for` 在外层兜底,即使工作线程卡死,主循环也按时返回。

```python
# 之前 — 偶发死锁
async with httpx.AsyncClient() as client:
    resp = await asyncio.wait_for(client.post(...), timeout=20)

# 之后 — 跨平台稳定
def _sync_request():
    with httpx.Client() as client:
        return client.post(..., timeout=15)
resp = await asyncio.wait_for(asyncio.to_thread(_sync_request), 20)
```

#### 坑 2:lark-oapi 回调阻塞 → 飞书重推风暴

第一版飞书 ws handler 直接 `asyncio.run(run_turn(text))`,LLM 跑 8 秒,SDK 主循环这 8 秒不发 ACK,**飞书认为我们死了开始重推**,同一条消息 5 分钟内推 3 次,bot 回 3 遍。

**修法**:回调函数立刻返回(< 1ms),实际任务丢到工作线程跑;再加一个 `message_id` 去重(24h TTL 内存 dict + threading lock)兜底。

#### 坑 3:bot 拿工具的 JSON 当回复"复制粘贴"

加完 `memory` 工具,用户说"我是 AI 专业的",bot 居然回复:
```json
{"success": true, "target": "user", "entries": ["aa 的专业是 AI"]}
```

LLM 把工具返回值当成"漂亮的回复内容"贴出去了。改 prompt 加"不许返回 JSON"没用。

**最终解法**:把工具返回值改成 `"ok"`——**LLM 没东西可抄,只能自己写真回复**。few-shot 示范放在 system prompt 里教它"调完 memory 后,要用自然语言确认"。这种"工具返回越像散文,LLM 越爱抄"是 deepseek 的小模型特性。

#### 坑 4:per-user 记忆隔离 → 跨进程怎么传 user_id

群里多个用户跟同一个 bot 聊,得各人记各人的事。tool-agent 是子进程,只能拿到 task 文本——怎么知道当前消息是哪个用户?

**方案**:`LANGCHAIN_AGENT_MEMORY_USER` 环境变量。gateway 在 `MCPHost.spawn` 之前 `os.environ[...] = user_id`,**子进程继承 env**,tool_memory 内部读这个 env 决定写到 `memories/users/<sha256>/USER.md` 哪个目录。turn 结束恢复原 env,REPL 那边的全局记忆不受影响。

代价:一次只能跑一个 turn(因为 env 是 process-global)。用 `_CONCURRENCY_GUARD: asyncio.Lock` 串行化即可——chat-bot 场景一秒一条够用。

---

## 六、可观测性 / 防御性编程

| 类别 | 做法 |
|---|---|
| 跨进程日志 | tool-agent 通过 `emit_event` 写 `.agent/runtime/telemetry.ndjson`,orchestrator tail 这个文件实时展示 |
| Gateway 日志 | 启动时 attach FileHandler 到 `gateway.log`,REPL 启的 gateway 跟 standalone 走同一份文件,`tail -f` 调试方便 |
| PID lock | `.langchain-agent/<platform>.pid`,防止同一 bot 同时被两个 gateway 进程接管 → 重复回复 |
| 消息去重 | message_id 内存 LRU(QQ 5 分钟、Feishu 24 小时),应对服务端重推 |
| Reply 截断 | 飞书 8KB / QQ 3500 字符 cap,超出加"已截断"后缀。服务端 silent reject 长消息会变成 bot "失声" |
| 错误外显 | LLM 失败 → bot 回复 `[error] ...` 让用户看到具体错误,而不是干瞪眼 |

---

## 七、技术栈

```
LangChain 0.3 / LangGraph 0.2     编排
mcp >= 1.0                        子进程协议
a2a-sdk >= 0.2                    任务级 SSE 流式委托
FastAPI / uvicorn                 飞书 webhook + A2A server
httpx                             同步 + 异步 HTTP 客户端
websockets >= 12                  QQ 官方机器人 ws
lark-oapi >= 1.4                  飞书 SDK + 长连接
pyjwt                             跨进程 token
cryptography                      Feishu 加密模式 AES-256-CBC
pytest + pytest-cov               测试
Rich + prompt_toolkit             TUI
```

模型 provider 都注册在 `config/_providers.py`:OpenAI、Anthropic、DeepSeek、Gemini、xAI、Moonshot、阿里、腾讯 TokenHub、OpenRouter、AI Gateway 等 20+ 个,统一抽象成 OpenAI-chat 或 Anthropic 两种协议。

---

## 八、规模 / 测试覆盖

```
源代码         ~1.5 万行 Python
单元测试       45 个 test 文件
gateway 测试   116 个用例,helper 模块覆盖率 84-98%
e2e 测试       tests/test_e2e_multi_agent/(orchestrator + spawn 真子进程)
依赖           48 个直接依赖
```

---

## 九、我学到的 / 可以展开聊的

### 9.1 工程层

- **协议设计的取舍**:MCP 同步 vs A2A 流式,什么场景下用哪个。同步 RPC 适合"一次性结果"型工具(`read_file`);流式 SSE 适合"长跑 + 中间反馈"任务(`tool.task` 跑 ReAct loop)。
- **跨平台陷阱**:Windows 的 ProactorEventLoop + IOCP 跟 Linux 的 SelectorEventLoop + epoll 不一样,共用一个 event loop 跑 ws + http 在 Windows 上踩坑。`asyncio.to_thread` 是治本方案。
- **DRY 的边界**:一开始飞书 webhook 跟 ws 两套 adapter 各自维护,几轮迭代后发现两边漏修(尤其 webhook 漏了 session/memory 接入)。抽 `_feishu_common.py` 之后,**两个 adapter 用同一份语义,只剩传输层差异**——这是 DRY 真正有价值的应用,不是为了少几行代码,而是为了**不让两边漂移**。

### 9.2 跟 LLM 打交道

- **小模型对 prompt 字面意思敏感**:工具返回值长得像散文,deepseek 就抄;返回 `"ok"`,deepseek 才会自己写。few-shot 示范比负面禁令(`don't paste JSON`)管用得多。
- **planner 路由策略**:`tool.task` 当 fallback default,具体 capability(`calculator`、`memory`)需要 planner 明确判断;但**`memory` 不能直接暴露给 planner**(否则它会绕过 ReAct loop,直接调,然后把 `"ok"` 当回复)。所以加进 `_INTERNAL_ONLY`,只有 tool-agent 的 ReAct loop 能用。
- **身份认同**:模型一上来不知道自己是谁,问"你是谁"会编"我是 DeepSeek 运行在启元魔方平台"。在 system prompt 顶部钉一句 `You are WW Agent, ...` 治好。

### 9.3 安全 / 信任边界

- **JWT 短 TTL 比 long-lived secret 好**:每次 token 60s,被泄漏窗口短,且 `allowed_tools` 字段把权限精确到工具
- **subprocess env 脱敏**:常见疏忽——LangChain `subprocess.Popen` 默认继承 env,bot 调 `pip install` 会把 OPENAI_KEY 之类泄漏给 pip 的下载链路;主动 deny 比白名单更稳

---

## 十、未来 roadmap

按优先级:

1. **QQ 过审上正式**:沙箱出站消息审核严格,影响测试体验;过审后切 `sandbox=false`
2. **集成测试**:单测覆盖了 helper,但 manager.start_* / runner._run_turn_locked 这种异步集成路径还得手测。需要 mock httpx + uvicorn + websockets 才能写
3. **Metrics**:每分钟回复数 / LLM token 用量 / P95 延迟,Prometheus 或 ndjson 滚动统计
4. **lark-oapi SDK 干净 stop**:当前 ws 线程没法干净 cancel,REPL stop 之后线程仍在跑——SDK 限制,需要等上游
5. **更多平台**:DingTalk、企业微信(WeCom)、Discord——只要照 `gateway/feishu_ws.py` 的模板新建一个文件
6. **横向并发**:目前所有 turn 全局串行(`_CONCURRENCY_GUARD`),将来要扛量得改成 per-user 隔离 sub-loop

---

## 附:面试可能问到的问题(自答案)

**Q: 为什么不直接用 LangGraph 的 multi-agent supervisor 模式?**
A: 用了,orchestrator 内部就是 LangGraph 状态机。但 supervisor 是单进程的,工具调用越权风险大;multi-process + JWT 是真正可放心给生产用的隔离。

**Q: 跨进程 IPC 为什么不直接用 gRPC?**
A: MCP 已经是社区标准的 LLM 工具 IPC 协议,生态(IDE 插件、调试工具)都跟它对齐;A2A 用 SSE 是为了流式吐 token——gRPC streaming 也能做但 SSE 跟 web 工具(curl、浏览器)调试友好得多。

**Q: 飞书 + QQ 都用 ws 长连接,扩展性怎么办?**
A: 单实例本身就能跑一个 bot 完整业务(QPS 上限是 LLM 调用速度,不是网络);真要扛量是水平扩展——一个 bot 一个进程,前面挂负载均衡的方案在 `python -m gateway` 模式下天然支持。

**Q: 跟 hermes-agent 比为什么砍掉那么多?**
A: hermes 是平台级产品(支持镜像、跨平台路由、用户档案、cron),我们的定位是"够用的 agent + IM 桥",90% 用户场景用不上的复杂度都砍了。如果要做平台级,需要补:多用户 session 管理、跨设备身份、外部 SQL 存档——预计还要 5000-1 万行。

---

*作者:[你的名字]*
*项目仓库:[填上]*
*Demo / 截图:可以现场跑 `python cli.py` 或 `python -m gateway feishu` 演示*
