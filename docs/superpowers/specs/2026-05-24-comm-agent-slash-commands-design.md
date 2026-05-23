# 设计:comm-agent 斜杠命令(REPL 直驱)

日期:2026-05-24
状态:已批准设计,待实现

## 1. 背景与目标

当前 comm-agent 的 `comm.*` 工具只能靠 orchestrator 的 planner LLM 从自然语言里识别意图后调用。用户反馈:**纯靠语言识别意图不可靠**,需要在 REPL 里加显式斜杠命令来:

1. 注册 / 管理远程对端;
2. 把一句话明确"通讯过去"(委派任务或对话),不经过 planner。

斜杠命令绕过 planner,直接通过 MCP host 调用 comm-agent 子进程里的 `comm.*` 工具,行为确定、可预期。

## 2. 命令集

全部在 `orchestrator/repl_commands.py` 的 `ReplCommandHandler` 里实现,通过 `handle()` 的命令分发接入。

| 命令 | 行为 |
|---|---|
| `/comm add` | 交互式逐项提问注册对端(密钥隐藏输入,可选自签指纹);成功后自动设为"当前对端" |
| `/comm list` | 列出已注册对端;`★` 标记当前对端 |
| `/comm use <name>` | 切换当前对端;名字不存在则报错 |
| `/comm rm <name>` | 删除对端;若删的是当前对端,清空当前对端与其对话上下文 |
| `/task <一句话>` | 把这句话作为**任务**委派给当前对端(`comm.delegate`) |
| `/chat <一句话>` | 把这句话作为**对话**发给当前对端(`comm.chat`,自动续传 context_id) |

未识别的 `/comm <子命令>` → 提示可用子命令。`/comm` 不带子命令 → 显示简短用法。

## 3. 会话状态(内存,session 级)

存在 `ReplCommandHandler` 实例上(该实例在 REPL 启动时构造一次、整轮复用,故无需改动 `state`/`controller`):

- `self._current_peer: str | None` —— 当前对端 peer_id。
- `self._chat_contexts: dict[str, str]` —— 每个 peer_id 对应的 `context_id`。

规则:
- 注册成功(`ok=true`)后自动把 `_current_peer` 设为新对端。
- `/chat` 首轮 `context_id=None`,把返回的 `context_id` 存进 `_chat_contexts[peer]`,后续自动带上;切换对端各自独立。
- `/comm rm <当前对端>` → `_current_peer=None`,并 `pop` 掉其 `context_id`。
- `/comm use <不存在>` → 报错,不改当前对端。
- `/task`、`/chat` 在 `_current_peer is None` 时 → 提示"先 `/comm add` 或 `/comm use <name>`"。
- **不持久化**:重启 REPL 后需重新 `use`。理由:YAGNI,会话级状态足够。

## 4. 底层调用

- 统一通过 `await self.host.call_tool("comm-agent", "comm.<tool>", args)`,从返回 `result.content[0].text` 取 JSON 字符串后 `json.loads` 再渲染。`"comm-agent"` 来自 card id,定义为模块常量。
- comm-agent 未运行 / 调用异常时,捕获并渲染友好提示("comm-agent 未运行,检查 `.agent/agents/` 与启动日志"),不抛出。

各命令对应的底层工具:

| 斜杠命令 | 底层工具 | 关键参数 |
|---|---|---|
| `/comm add` | `comm.add_peer` | `peer_id`, `url`, `hmac_secret_value`, `display_name?`, `tls_verify?`, `tls_pinned_sha256?` |
| `/comm list` | `comm.list_peers` | — |
| `/comm rm` | `comm.remove_peer` | `peer_id` |
| `/task` | `comm.delegate` | `peer_id`, `task`, `stream=false` |
| `/chat` | `comm.chat` | `peer_id`, `message`, `context_id?` |

`/comm use` 先用 `comm.list_peers` 校验名字存在,存在才更新本地当前对端,否则报错(见 §3)。

## 5. 扩展底层 `comm.add_peer`(`agents/comm_agent/mcp_tools.py`)

为支持自签证书,给 `add_peer` 处理器新增两个可选入参:

- `tls_verify`(默认 `True`)
- `tls_pinned_sha256`(默认 `None`)

构造 `Peer` 时用这两个值替换原先硬编码的 `tls_verify=True / tls_pinned_sha256=None`。注册表已有校验(`verify=false` 必须配 `pinned_sha256`,否则 `PeerRegistryError`),`add_peer` 已捕获并返回 `{ok:false,error}`,无需额外处理。

`/comm add` 交互流程收集:`peer_id` → `url` → `display_name`(可空)→ "是否自签证书?"(y/N);若 y 则收一行 SHA-256 指纹,以 `tls_verify=False` + `tls_pinned_sha256=<指纹>` 传入;否则用默认(标准 CA 校验)→ 最后隐藏输入 HMAC 密钥。

## 6. 渲染

- `/task`:用 `stream=false` 调用,显示**目标对端 + 最终结果 + 耗时**。说明:底层 `comm.delegate` 为 MVP 的"一次性返回完整记录",非逐字推流,因此展示的是任务跑完后的结果,不是实时滚动。
- `/chat`:显示**目标对端 + reply**,并记下 `context_id`。
- `/comm list`:表格(对端名、display_name、URL、`★`当前标记)。
- **防误发**:`/task`、`/chat` 的输出顶部回显 `→ 发送给 <peer> (<url>)`,让目标始终可见。
- 斜杠命令输出沿用现有 `ui.render_text` / `render_table` / `render_command_error` 风格(不套 Panel 框,框留给运行态错误)。

## 7. 边界与错误

- 交互式 `/comm add` 需要 TTY(与现有 `/model`、`/gateway` 一致);非 TTY 时给提示并中止。
- **长任务局限**:`/task` 是同步 `await` 调用,委派长任务时(a) REPL 输入阻塞至返回;(b) 受 `A2AClient` 默认 30s 超时限制,超时则失败。后台化(类似 `/gateway`)留作后续,本期不做。
- comm 工具本身永不抛(返回 `{ok:false,error}`),命令层把它渲染成友好错误。
- 在 `COMMANDS` 注册表(`/help` 与输入补全用)补 `/comm`、`/task`、`/chat` 的帮助文本。

## 8. 可测试性

把 `/comm add` 拆成两层:
- **交互取值层**(薄):TTY 下用 `rich.prompt.Prompt` 逐项取值,与现有 `/model`/`/gateway` 一致,不做单测。
- **执行层**(可测纯逻辑):接收已收集好的参数 → 调 `host.call_tool` → 解析 → 渲染 + 更新当前对端。

测试用例:

`tests/test_orchestrator/`(新增,注入假的 `host.call_tool`):
- `/comm list` 渲染、`★` 当前标记。
- `/comm use <存在>` 设当前;`/comm use <不存在>` 报错且不改状态。
- `/comm rm <当前>` 清空当前对端与 context。
- `/task` 无当前对端时提示;有当前对端时以正确 `peer_id`+`stream=false` 调用并回显目标。
- `/chat` 首轮 `context_id=None`,续传时带上次返回的 `context_id`;切换对端 context 独立。
- comm-agent 调用异常时渲染友好错误、不抛。

`tests/test_comm_agent/test_mcp_tools.py`(扩展):
- `comm.add_peer` 传 `tls_pinned_sha256` + `tls_verify=False` 时,注册表落地为嵌套 `tls.verify=false / pinned_sha256=<指纹>`。
- 不传时维持默认 `tls_verify=True`。

## 9. 改动文件清单

- `agents/comm_agent/mcp_tools.py` —— `add_peer` 新增 `tls_verify` / `tls_pinned_sha256` 参数。
- `orchestrator/repl_commands.py` —— `/comm` 子命令 + `/task` + `/chat` + 会话状态字段。
- `orchestrator/repl_ui.py`(或 `COMMANDS` 所在处)—— 补帮助文本。
- `tests/test_orchestrator/` —— 斜杠命令测试。
- `tests/test_comm_agent/test_mcp_tools.py` —— add_peer 指纹分支测试。

## 10. 范围之外

- 后台化的长任务委派(本期同步阻塞)。
- 持久化"当前对端"(本期内存)。
- `/task` 真·逐字流式展示(受限于 `comm.delegate` MVP 的一次性返回)。
- 多 comm-agent 实例 / 动态 agent_id(本期固定 `comm-agent`)。
