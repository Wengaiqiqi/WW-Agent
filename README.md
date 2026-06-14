# W&W Agent CLI

> 跑在你电脑上的多智能体 AI 助手——读写文件、执行命令、上网搜索、看图、接飞书/QQ 当机器人，支持远程 Agent 协作。

---

## 目录

- [功能特性](#功能特性)
- [架构概览](#架构概览)
- [快速开始](#快速开始)
- [配置模型](#配置模型)
- [斜杠命令](#斜杠命令)
- [工具列表](#工具列表)
- [权限模式](#权限模式)
- [技能系统](#技能系统)
- [聊天平台接入](#聊天平台接入)
- [远程 Agent 协作（A2A）](#远程-agent-协作a2a)
- [Web 界面](#web-界面)
- [环境变量](#环境变量)
- [开发与测试](#开发与测试)
- [项目结构](#项目结构)

---

## 功能特性

| 特性 | 说明 |
|------|------|
| **多智能体架构** | 中心编排器协调多个专家子进程（工具 Agent、技能 Agent、通信 Agent） |
| **LLM 无关** | 支持 OpenAI、Anthropic、DeepSeek、小米 MiMo 及任意兼容 OpenAI 协议的自定义端点 |
| **全面的工具集** | 13 个内置工具：文件读写、终端命令、Web 搜索/抓取、图像分析、内存快照等 |
| **三级权限控制** | 只读 / 可写 / 完全放开，权限在授权边界强制执行 |
| **技能系统** | 通过 SKILL.md + 辅助脚本定义领域工作流，可热插拔 |
| **聊天平台桥接** | 飞书（Webhook/WebSocket）和 QQ 官方机器人，无需改代码 |
| **远程 Agent 协作** | HMAC 签名 A2A 协议，支持跨机器任务委托 |
| **Web 界面** | FastAPI + SPA 前端，支持 SSE 流式响应和会话管理 |
| **中文优先** | 完整 UTF-8 支持，界面与文档均有中文版 |
| **安全加固** | SSRF 防护、命令输出敏感信息过滤、凭据文件 0600 权限 + 自动 .gitignore 防提交 |

---

## 架构概览

```
┌────────────────────────────────────────────────────────┐
│              Orchestrator（主 REPL 控制器）              │
│   LLM Planner（路由）  ·  Permission Gate（授权）       │
│   MCP Host（子进程管理）  ·  A2A Client（远程委托）      │
└──────────┬──────────────┬──────────────┬───────────────┘
           │              │              │
      ┌────▼────┐   ┌──────▼──────┐   ┌──▼──────┐
      │Tool     │   │Skill        │   │Comm     │
      │Agent    │   │Agent        │   │Agent    │
      │(ReAct)  │   │(SKILL.md)   │   │(A2A)    │
      └────┬────┘   └──────┬──────┘   └──┬──────┘
           │               │             │
      工具执行          领域工作流      远程对端
  (文件/Web/命令/图像)   (JSON信封协议)  (HMAC签名)
```

**通信协议**：Agent 之间通过 **MCP（Model Context Protocol）** 进行工具调用，远程 Agent 之间使用 **A2A 协议**进行任务委托。

---

## 快速开始

### 环境要求

- Python 3.11+
- Windows（PowerShell）/ Linux / macOS

### 安装

```powershell
# 1. 克隆项目
git clone <repo-url>
cd "W&W Agent"

# 2. 安装（自动创建 .venv 虚拟环境）
.\install.ps1

# 3. 激活环境
.venv\Scripts\Activate.ps1
```

Linux/macOS：

```bash
pip install -e .
# 或使用 uv
uv sync
```

### 启动

```powershell
python cli.py
```

**首次启动**会自动弹出模型配置向导（四步：选供应商 → 选模型 → 填 API Key → 填 base URL）。API Key 保存到本地凭据文件后，后续启动无需重填。

### 示例对话

```
ww-agent> 帮我把当前目录的 .py 文件列出来
ww-agent> 搜索「LangGraph 是什么」并总结三点
ww-agent> 读取 README.md 并用中文解释主要功能
ww-agent> 运行 python tests/test_file_ops.py 并告诉我结果
```

### 单次执行模式

```powershell
python cli.py prompt "列出当前目录的所有 Python 文件"
```

---

## 配置模型

使用 `/model` 命令进入交互式向导，支持以下供应商：

| 供应商 | 协议 | 说明 |
|--------|------|------|
| **Anthropic** | Anthropic | Claude 系列（默认） |
| **OpenAI** | OpenAI | GPT / o1 / o3 系列 |
| **DeepSeek** | OpenAI 兼容 | DeepSeek-V3 / R1 |
| **小米 MiMo** | OpenAI 兼容 | MiMo-7B-RL |
| **自定义** | OpenAI 兼容 | 任意自托管端点 |

也可通过环境变量直接覆盖：

```bash
export LANGCHAIN_AGENT_MODEL="anthropic/claude-opus-4-8"
export LANGCHAIN_AGENT_MODEL="openai/gpt-4o"
export LANGCHAIN_AGENT_MODEL="deepseek/deepseek-chat"
```

配置文件存储在 `.langchain-agent/settings.json`，API Key 存储在 `.langchain-agent/credentials.json`（文件设为 0600 权限，并自动写入同目录 .gitignore 防止误提交）。

---

## 斜杠命令

在 REPL 输入框中以 `/` 开头的均为命令，不参与对话：

| 命令 | 说明 |
|------|------|
| `/help` | 查看所有命令 |
| `/model` | 重新配置模型（换供应商 / 换模型 / 改 Key） |
| `/status` | 查看当前会话状态（模型、轮次、权限等） |
| `/config` | 查看当前生效配置 |
| `/tools` | 列出助手可用的所有工具 |
| `/skills` | 列出已安装的技能 |
| `/agents` | 列出后台的专家子进程 |
| `/instructions` | 列出已加载的项目说明文件 |
| `/permissions [mode]` | 查看 / 切换权限模式 |
| `/gateway` | 配置并启动飞书/QQ 机器人 |
| `/comm list\|add\|use\|rm` | 管理远程协作对端 |
| `/task <query>` | 委托任务给远程 Agent |
| `/chat <message>` | 与远程 Agent 对话 |
| `/clear` | 清空当前会话历史 |
| `/exit` | 退出程序 |

---

## 工具列表

工具实现统一位于 `tool/tool_*.py`，共 13 类：

### 文件操作

| 工具 | 说明 |
|------|------|
| `read_file` | 读取文件内容（支持文本、PDF、DOCX、图像） |
| `write_file` | 写入/创建文件 |
| `edit_file` | 精确字符串替换编辑 |
| `list_directory` | 列出目录内容（支持递归） |
| `glob_search` | 文件名模式匹配搜索 |
| `grep_search` | 文件内容正则搜索 |

### Web 访问

| 工具 | 说明 |
|------|------|
| `web_search` | 网络搜索（百度 / Startpage / DuckDuckGo / Tavily 四引擎，自动降级） |
| `web_extract` | 提取网页正文内容 |
| `web_crawl` | 深度抓取网页链接树 |

### 执行

| 工具 | 说明 |
|------|------|
| `run_command` | 执行 Shell 命令（默认 180s 超时） |
| `run_python` | 执行 Python 代码片段 |

### 其他

| 工具 | 说明 |
|------|------|
| `vision_analyze` | 图像内容分析（调用视觉模型） |
| `memory` | 读写持久化内存快照（注入到系统提示） |
| `clarify` | 向用户请求澄清信息 |
| `calculator` | 安全表达式求值 |
| `mixture_of_agents` | 多模型融合推理 |

> **安全特性**：`web_extract`/`web_crawl` 内置 SSRF 防护，拒绝访问私有 IP、回环地址和云元数据端点；`run_command` 会过滤输出中的 API Key 等敏感信息。

---

## 权限模式

系统内置三种权限模式，控制哪些工具可用：

| 模式 | 可用工具 | 适用场景 |
|------|----------|----------|
| `read-only` | 文件读取、Web 搜索/抓取、查询类工具 | 只需要信息查询，防止意外修改 |
| `workspace-write`（默认） | read-only + 文件写入/编辑、受限 Shell | 日常开发任务 |
| `danger-full-access` | 全部工具，含 Home Assistant 等 | 自动化、IoT 控制 |

切换方式：

```
ww-agent> /permissions read-only
ww-agent> /permissions workspace-write
ww-agent> /permissions danger-full-access
```

也可通过环境变量设置：

```bash
export LANGCHAIN_AGENT_PERMISSION_MODE=read-only
```

---

## 技能系统

技能是可插拔的领域工作流，由 `SKILL.md`（指令文档）+ `_meta.json`（元数据）+ 辅助脚本组成。

### 目录结构

```
skills/<slug>/
├── SKILL.md          # 领域指令与工作流说明（作为系统提示注入）
├── _meta.json        # 元数据：关键词、所需工具、环境变量
└── scripts/          # 辅助 Python 脚本（通过 tool-agent 调用）
    ├── search.py
    ├── compare.py
    └── ...
```

### `_meta.json` 格式

```json
{
  "matchKeywords": ["关键词1", "keyword2"],
  "requiresTools": ["web_search", "run_python"],
  "requiresEnv": ["MY_SKILL_TOKEN"]
}
```

### 内置技能

| 技能 | 说明 |
|------|------|
| `baidu-ecommerce-search` | 百度电商搜索：商品检索、价格比较、品牌排名、下单流程 |

### 添加自定义技能

1. 在 `skills/` 下创建新目录（如 `skills/my-skill/`）
2. 编写 `SKILL.md` 定义工作流
3. 编写 `_meta.json` 声明依赖
4. 重启 Agent，使用 `/skills` 查看是否已加载

---

## 聊天平台接入

### 飞书/Lark 机器人

通过 `/gateway` → 选择飞书 → **Setup credentials** 进入交互式配置向导。

#### 第一步：选择连接模式

| 模式 | 说明 |
|------|------|
| **ws**（推荐） | WebSocket 长连接，机器人主动连出，无需公网地址 |
| **webhook** | 飞书将事件 POST 到你的服务器，需要公网可访问的 URL |

#### 第二步：填写凭据

| 字段 | 必填 | 说明 |
|------|------|------|
| `app_id` | ✅ | 飞书开放平台的 App ID（`cli_xxxx`） |
| `app_secret` | ✅ | App Secret |
| `domain` | ❌ | `open.feishu.cn`（国内）或 `open.larksuite.com`（海外），默认前者 |
| `allowed_users` | ❌ | 逗号分隔的授权 open_id 列表；留空则无人可用 `/chat` `/task` |

#### Webhook 模式额外字段

仅当连接模式选为 `webhook` 时才需要填写：

| 字段 | 必填 | 说明 |
|------|------|------|
| `verify_token` | ✅ | 事件订阅的 Verification Token（在飞书开放平台「事件订阅」页面获取） |
| `encrypt_key` | ❌ | Encrypt Key（留空表示不加密） |
| `reply_in_thread` | ❌ | 是否在话题中回复（`y`/`n`），默认否 |
| `host` | ❌ | Webhook 监听地址，默认 `0.0.0.0` |
| `port` | ❌ | Webhook 监听端口，默认 `8765` |

#### 启动

```
ww-agent> /gateway
# 选择飞书 → 配置凭据 → 启动
```

或直接运行：

```bash
python -m gateway feishu --port 8765
```

---

### QQ 官方机器人

通过 `/gateway` → 选择 QQ → **Setup credentials** 进入交互式配置向导。

#### 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `app_id` | ✅ | QQ 开放平台的 Bot AppID |
| `client_secret` | ✅ | Bot Client Secret |
| `intents` | ❌ | Intents 位掩码，**留空使用默认值**即可。默认 = `C2C+Group@+Channel@`（接收私聊、群聊和频道中 @机器人的消息）。仅当需要接收频道私信时才需手动填写 |
| `sandbox` | ❌ | 是否使用沙箱测试环境（`y`/`n`），**默认 `n`（正式环境）**。除非你在使用腾讯的沙箱测试频道，否则填 `n` |
| `allowed_users` | ❌ | 逗号分隔的授权 openid 列表；留空则无人可用 `/chat` `/task` |

#### 启动

```
ww-agent> /gateway
# 选择 QQ → 配置凭据 → 启动
```

或直接运行：

```bash
python -m gateway qq
```

> 凭据保存在 `.langchain-agent/gateways.json`（自动写入同目录 .gitignore 防止误提交）。

---

## 远程 Agent 协作（A2A）

允许两个 Agent 实例跨机器协作，通过 HMAC 签名的 A2A 协议委托任务。

### 添加远程对端

```
ww-agent> /comm add
```

会逐项提示填写（Ctrl+C 取消）：

| 字段 | 必填 | 说明 |
|------|------|------|
| `peer_id` | ✅ | 对端唯一标识，例如 `hermes-server` |
| `url` | ✅ | 对端地址，例如 `https://8.163.112.21:8443`（优先 https） |
| `display_name` | | 显示名，留空则同 `peer_id` |
| `Self-signed certificate?` | | 对端用自签证书才选 `y`，然后填 SHA-256 指纹 |
| `HMAC secret` | ✅ | 和对端约定的共享密钥（输入时隐藏） |

> **HMAC 密钥不落盘**，只写入当前进程环境变量。注册成功时会提示一条 `export COMM_PEER_<名>_HMAC=<值>`，重启后想免重填就把这条加到 shell profile。

### 使用远程 Agent

```
ww-agent> /task 帮我分析一下这份日志文件
ww-agent> /chat 你那边的数据库状态怎么样？
```

### 对端配置示例

```
ww-agent> /comm list        # 列出所有对端
ww-agent> /comm use prod    # 切换到 prod 对端
ww-agent> /comm rm dev      # 删除 dev 对端
```

安全机制：所有跨 Agent 调用使用 HMAC 签名 Grant + JWT 验证，防止未授权委托。

---

## Web 界面

提供基于浏览器的 SPA 界面，支持 SSE 流式响应。

### 启动

```powershell
python web/__main__.py
# 或
.\start_web.bat
```

默认访问 `http://localhost:8000`

### 功能

- 流式对话界面（实时渲染 LLM 输出）
- 会话管理与历史记录（SQLite 存储）
- 模型配置 API（`/api/config`）
- JWT 身份认证 + 速率限制

---

## 环境变量

### 核心控制

| 变量 | 说明 | 示例 |
|------|------|------|
| `LANGCHAIN_AGENT_MODEL` | 覆盖模型选择 | `anthropic/claude-opus-4-8` |
| `LANGCHAIN_AGENT_PERMISSION_MODE` | 覆盖权限模式 | `read-only` |
| `LANGCHAIN_AGENT_CONFIG_DIR` | 覆盖配置目录 | `/custom/path/.langchain-agent` |
| `LANGCHAIN_AGENT_WORKSPACE_ROOT` | 沙箱文件操作目录 | `/home/user/projects` |
| `LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS` | 允许访问私有 IP（仅开发用） | `true` |

### Provider API Key

| 变量 | 说明 |
|------|------|
| `ANTHROPIC_API_KEY` | Anthropic Claude |
| `OPENAI_API_KEY` | OpenAI GPT |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `XIAOMI_API_KEY` | 小米 MiMo |

> API Key 优先从 `credentials.json` 读取，也可直接设置环境变量覆盖。

### 搜索工具

| 变量 | 说明 |
|------|------|
| `TAVILY_API_KEY` | Tavily 搜索引擎（可选，与 DuckDuckGo 并联） |

### 聊天平台（Gateway）

| 变量 | 说明 |
|------|------|
| `QQ_APP_ID` | QQ 机器人 AppID（对应配置中的 `app_id`） |
| `QQ_CLIENT_SECRET` | QQ 机器人 Client Secret（对应配置中的 `client_secret`） |
| `QQ_INTENTS` | QQ Intents 位掩码（可选，对应配置中的 `intents`） |
| `QQ_SANDBOX` | 设为 `1` 使用 QQ 沙箱环境（可选，对应配置中的 `sandbox`） |

> 优先从 `/gateway` 交互式配置读取，环境变量仅作为备用。飞书凭据仅通过 `/gateway` 配置，不使用环境变量。

---

## 开发与测试

### 安装开发依赖

```bash
pip install -e ".[dev]"
```

### 运行测试

```bash
# 快速测试（跳过 E2E 子进程测试）
pytest -k "not e2e"

# 完整测试（含子进程启动）
pytest

# 带覆盖率
pytest --cov=. --cov-report=html
```

### 测试结构

| 路径 | 覆盖范围 |
|------|----------|
| `tests/test_e2e_multi_agent/` | 端到端子进程集成测试 |
| `tests/test_orchestrator/` | Planner、Router、Permission Gate |
| `tests/test_tool_agent/` | 工具执行、Workspace 边界 |
| `tests/test_skill_agent/` | 技能加载、JSON 信封解析 |
| `tests/test_shared/` | Mock 模型、AuthZ、遥测 |
| `tests/test_gateway/` | 飞书/QQ 适配器 |
| `tests/test_security/` | SSRF 防护、敏感信息过滤 |

### 代码质量

```bash
# 类型检查
mypy --strict agent_paths.py orchestrator/ agents/shared/

# 安全扫描
bandit -r . -ll

# 依赖漏洞检查
pip-audit
```

### 安装可选依赖

```bash
# 文档解析（PDF、DOCX、PPTX）
pip install -e ".[docs]"

# 语音合成
pip install -e ".[tts]"

# 图像生成
pip install -e ".[image-gen]"
```

---

## 项目结构

```
W&W Agent/
├── cli.py                      # 入口（argparse 分发）
├── pyproject.toml              # 项目元数据与依赖
├── install.ps1                 # Windows 一键安装脚本
├── start_web.bat               # 启动 Web 界面
├── agent.md                    # 架构设计文档
├── 用户手册.md                  # 中文用户手册
│
├── prompt_rules.py             # 跨 Agent 共享提示规则
├── agent_display.py            # 工具调用渲染逻辑
├── agent_paths.py              # 配置目录解析
├── project_context.py          # 项目指令文件发现
│
├── config/                     # 模型配置与凭据管理
│   ├── _providers.py           # Provider 注册表
│   ├── _settings.py            # settings.json 读写
│   ├── _credentials.py         # 凭据存储（0600 权限 + .gitignore 防提交）
│   └── _llm.py                 # LangChain ChatModel 工厂
│
├── orchestrator/               # 中心编排器
│   ├── main.py                 # 启动、MCP Host、REPL 入口
│   ├── turns.py                # LLM Planner（任务路由）
│   ├── router.py               # CapabilityRouter
│   ├── permission_gate.py      # 工具授权检查
│   ├── repl_controller.py      # REPL 主循环
│   ├── repl_ui.py              # Rich TUI 渲染
│   ├── mcp_host.py             # Agent 子进程管理
│   ├── telemetry.py            # 事件流日志
│   └── a2a_client.py           # 远程 Agent 客户端
│
├── agents/
│   ├── tool_agent/             # 工具 Agent（LangGraph ReAct）
│   │   ├── agent_loop.py       # ReAct 循环（766 行）
│   │   └── tool_executor.py    # MCP 工具包装
│   ├── skill_agent/            # 技能 Agent
│   │   └── skill_executor.py   # JSON 信封协议执行（679 行）
│   ├── comm_agent/             # 通信 Agent（A2A）
│   │   ├── main.py
│   │   ├── a2a_protocol.py
│   │   └── peer_registry.py
│   └── shared/                 # 共享基础设施
│       ├── authz.py            # JWT + HMAC 授权
│       ├── mcp_server.py       # MCP 服务器基类
│       ├── a2a_server.py       # A2A 流式协议
│       ├── permission_modes.py # 三级权限定义
│       └── mock_chat_model.py  # 测试用 Mock LLM
│
├── tool/                       # 工具实现（唯一真相源）
│   ├── tool_file_ops.py        # 文件操作（含 Workspace 边界）
│   ├── tool_shell.py           # Shell 执行（含超时/过滤）
│   ├── tool_web.py             # Web 访问（含 SSRF 防护）
│   ├── tool_memory.py          # 持久化内存
│   ├── tool_vision.py          # 图像分析
│   ├── tool_basic.py           # 基础工具（计算、时间）
│   └── tool_moa.py             # 多模型融合
│
├── skills/                     # 技能包
│   └── baidu-ecommerce-search/ # 百度电商搜索技能
│       ├── SKILL.md
│       ├── _meta.json
│       └── scripts/
│
├── gateway/                    # 聊天平台适配器
│   ├── feishu.py               # 飞书 Webhook
│   ├── feishu_ws.py            # 飞书 WebSocket
│   ├── qq.py                   # QQ 官方机器人
│   └── README.md
│
├── web/                        # Web 界面（FastAPI）
│   ├── app.py                  # FastAPI 应用工厂
│   ├── bridge.py               # Web↔Orchestrator 桥接
│   ├── store.py                # SQLite 会话存储
│   ├── auth.py                 # JWT 会话认证
│   └── static/                 # 前端 SPA 资源
│
├── bridge/hermes_a2a/          # 远程 Agent 协作协议
│
└── tests/                      # 测试套件（77 个文件）
    ├── test_e2e_multi_agent/
    ├── test_orchestrator/
    ├── test_tool_agent/
    ├── test_skill_agent/
    ├── test_shared/
    ├── test_gateway/
    └── test_security/
```

---

## License

本项目保留所有权利。详见 `LICENSE` 文件。
