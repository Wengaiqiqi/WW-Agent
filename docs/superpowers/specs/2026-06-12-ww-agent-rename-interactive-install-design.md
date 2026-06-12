# 设计文档：交互式安装 + 全量改名 + 修 peer_id 不匹配坑

- 日期：2026-06-12
- 范围：`scripts/` 安装脚本、`agents/comm_agent/`、`bridge/hermes_a2a/`、相关测试与 README
- 状态：已确认，待写实现计划

## 背景与问题

项目历史名为 `agent-last`，现统一更名为 **W&W Agent**。同时之前遇到一个安装期阻塞点：

> bridge 返回 `caller peer not allowed`，agent-last 侧显示 empty reply。
> 根因：安装时填 `--your-peer-id agent-last`，但本机 comm-agent 默认自报
> `my_peer_id = "agent-last-laptop"`，远端白名单 `HERMES_A2A_ALLOWED_PEER`
> 与本机自报值对不上。

命名约定：
- **代码标识符**（peer_id、env 值、变量名）用安全 slug **`ww-agent`**。
- **对外显示名**（description、organization、文档标题）用 **`W&W Agent`**。
- 之所以不能直接拿 "W&W Agent" 当 peer_id：它带空格和 `&`，会出现在 URL / HTTP header / JWT subject 中，会出错。

## 目标

1. 修掉 `caller peer not allowed`：让本机默认 `my_peer_id` 与安装脚本默认 `your-peer-id` 天然一致。
2. 4 个安装脚本改为**交互式 + 向后兼容**的混合模式。
3. 全量把 `agent-last` / `agent-last-laptop` 改为 `ww-agent` / `W&W Agent`，不改目录结构。

## 非目标（YAGNI）

- 不重命名目录（`agents/comm_agent/` 等本就不含 `agent-last`）。
- 不引入"装机时双方协商写入两边配置"的握手机制（脚本跑在远端，拿不到本机 env，无法真校验）。
- 不改端口、HMAC 算法、A2A 协议本身。

## 块 1：修 peer_id 不匹配（核心）

策略：**靠默认值天然对齐消除坑**，并辅以显式提示。

- 本机默认 `COMM_AGENT_MY_PEER_ID` → `ww-agent`
  （`agents/comm_agent/main.py` 中的 `os.environ.get("COMM_AGENT_MY_PEER_ID", "ww-agent")`）
- 4 个安装脚本中 `--your-peer-id` / `-YourPeerId` 的**默认值设为 `ww-agent`**，
  与本机默认相等 → 全程回车也必然一致。
- 脚本结尾打印校验提醒：「本机需保证 `COMM_AGENT_MY_PEER_ID` = 你填的 your-peer-id
  （默认 `ww-agent`），否则会 `caller peer not allowed`」。

为什么不做自动校验：安装脚本运行在**远端机器**（Hermes/OpenClaw 那台），无法访问本机
laptop 的环境变量，因此无法在安装时验证两边一致。默认值对齐 + 明确提示是最务实方案。

## 块 2：交互式安装（混合模式，4 个脚本全改）

涉及脚本：
- `scripts/install_hermes_a2a.ps1` / `.sh`
- `scripts/install_openclaw_a2a.ps1` / `.sh`

行为：
- **不带参数运行** → 逐项交互询问，每项带默认值，回车即用默认。
- **带任意参数运行** → 沿用现有命令行行为（向后兼容，CI/自动化可用）。

交互项与默认值：

| 项 | 默认 | 说明 |
|---|---|---|
| MyPeerId（远端自己的 id） | `hermes-home` / `openclaw-home` | 远端这台的 peer_id |
| YourPeerId（本机 id） | `ww-agent` | 写进白名单，必须 = 本机 my_peer_id |
| PublicHost | 无默认，必填（留空则重问） | 对外主机名 |
| HmacSecret | 留空 → 自动生成 32 字节 hex 并打印 | 仅此一次打印 |
| 端口等 | 现有默认值 | 交互中可跳过 |

技术细节：
- `.sh`：用 `curl … | bash` 跑时 stdin 被脚本占用，普通 `read` 读不到键盘。
  交互读取改为从 `/dev/tty` 读（`read -r VAR < /dev/tty`）。当 `/dev/tty` 不可用
  （纯管道无终端）时，回退为"缺参数即报错退出，要求改用命令行参数"。
- `.ps1`：用 `Read-Host` 实现交互；把现有 `[Parameter(Mandatory=$true)]` 去掉，
  改为运行时检测空值再 `Read-Host`，HmacSecret 留空则自动生成。
- HMAC 自动生成：
  - `.sh`：`openssl rand -hex 32`
  - `.ps1`：`-join ((48..57)+(97..102) | Get-Random -Count 64 | %{[char]$_})`（hex）

## 块 3：全量改名 `agent-last` → `ww-agent` / `W&W Agent`

精确改动点（基于全仓搜索）：

默认值 / 逻辑：
- `agents/comm_agent/main.py:63` — 默认 `agent-last-laptop` → `ww-agent`
- `agents/comm_agent/main.py:12` — docstring 默认值说明
- `agents/comm_agent/main.py:83` — description `agent-last comm-agent` → `W&W Agent comm-agent`

显示文案 / 注释：
- `agents/comm_agent/agent_card.py:35-36` — organization `agent-last` → `W&W Agent`，
  url → `https://github.com/ww-agent/ww-agent`（占位，有真实仓库再替换）
- `agents/comm_agent/a2a_protocol.py:269` — 注释
- `bridge/hermes_a2a/__main__.py:3,8,38` — docstring / 报错文案中的 agent-last

脚本：
- `scripts/install_hermes_a2a.sh` / `.ps1`：变量 `AGENT_LAST_REPO`/`AGENT_LAST_DIR`
  → `WW_AGENT_REPO`/`WW_AGENT_DIR`，对应 env 名 `AGENT_LAST_*` → `WW_AGENT_*`，
  clone 目录 `~/.hermes-a2a/agent-last` → `~/.hermes-a2a/ww-agent`，所有提示文案
- `scripts/install_openclaw_a2a.sh` / `.ps1`：注释 / usage 中的 agent-last

文档：
- `agents/comm_agent/README.md` — 默认值表（`COMM_AGENT_MY_PEER_ID` 默认）、示例命令、
  排错表、白名单示例中的 `agent-last-laptop` → `ww-agent`

测试（"全改"且保持通过）：
- `tests/test_bridge_hermes/test_e2e_bridge.py:26,99` — `my_peer_id="agent-last-laptop"`
  → `"ww-agent"`
- `tests/test_bridge_hermes/test_e2e_bridge.py:1` — docstring
- `tests/test_comm_agent/test_agent_card.py:13,19` — `agent-last-comm` → `ww-agent-comm`
- `tests/test_bridge_hermes/conftest.py:15` — 注释里的示例路径 `D:\Claude Code\agent-last`
  → `ww-agent`

> 注：`test_agent_card.py` 的 name 是测试自传自断言的字符串，与实现解耦；改为
> `ww-agent-comm` 后测试仍通过。

## 验证

- 全仓 `grep -i 'agent-last'` 在改名完成后应只剩（若有）历史 git 记录之外的零命中。
- `pytest`（comm_agent + bridge_hermes 套件）全绿。
- `.ps1` 不带参数运行进入交互、带参数运行保持原行为；`.sh` 同理（含 `/dev/tty` 路径）。
- 安装脚本默认值 `your-peer-id = ww-agent` 与本机默认 `COMM_AGENT_MY_PEER_ID = ww-agent`
  一致，复现不再出现 `caller peer not allowed`。
