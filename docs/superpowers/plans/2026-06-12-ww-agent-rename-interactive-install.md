# W&W Agent 改名 + 交互式安装 + peer_id 修复 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把项目从 `agent-last` 全量更名为 `W&W Agent`（标识符 slug `ww-agent`），把 4 个安装脚本改成"交互式 + 向后兼容"，并通过统一默认 peer_id 消除 `caller peer not allowed`。

**Architecture:** 三类改动——(1) Python 源码默认值与显示文案；(2) 4 个安装脚本（`.sh`/`.ps1`）改为缺参数时交互、有参数时沿用旧行为；(3) 测试与文档跟随更名。核心修复是让本机默认 `COMM_AGENT_MY_PEER_ID` 与脚本默认 `--your-peer-id` 都等于 `ww-agent`，天然对齐。

**Tech Stack:** Python 3.11+ / pytest、Bash（`/dev/tty` 交互）、PowerShell（`Read-Host`）。

设计文档：`docs/superpowers/specs/2026-06-12-ww-agent-rename-interactive-install-design.md`

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `agents/comm_agent/main.py` | comm-agent 入口、默认 peer_id | Modify |
| `agents/comm_agent/agent_card.py` | agent card 构造（organization/url 文案） | Modify |
| `agents/comm_agent/a2a_protocol.py` | 注释文案 | Modify |
| `bridge/hermes_a2a/__main__.py` | bridge 入口 docstring/报错文案 | Modify |
| `tests/test_bridge_hermes/test_e2e_bridge.py` | 硬编码 peer_id | Modify |
| `tests/test_bridge_hermes/conftest.py` | 注释示例路径 | Modify |
| `tests/test_comm_agent/test_agent_card.py` | 硬编码 name 字符串 | Modify |
| `scripts/install_hermes_a2a.sh` | Hermes 桥接安装（交互式重写） | Rewrite |
| `scripts/install_hermes_a2a.ps1` | 同上（Windows） | Rewrite |
| `scripts/install_openclaw_a2a.sh` | OpenClaw 安装（交互式重写） | Rewrite |
| `scripts/install_openclaw_a2a.ps1` | 同上（Windows） | Rewrite |
| `agents/comm_agent/README.md` | 默认值表 / 示例 / 排错 | Modify |

执行前置：所有命令的工作目录为仓库根 `D:\Claude Code\W&W Agent`。当前分支应为 `feat/ww-agent-rename-interactive-install`（设计文档已提交在此分支）。

---

### Task 1: Python 源码默认值与文案改名

**Files:**
- Modify: `agents/comm_agent/main.py:12,63,83`
- Modify: `agents/comm_agent/agent_card.py:35-36`
- Modify: `agents/comm_agent/a2a_protocol.py:269`
- Modify: `bridge/hermes_a2a/__main__.py:3,8,38`

- [ ] **Step 1: 改 `main.py` 默认 peer_id（这是 `caller peer not allowed` 的核心修复点）**

`agents/comm_agent/main.py:63`，把：
```python
    my_peer_id = os.environ.get("COMM_AGENT_MY_PEER_ID", "agent-last-laptop")
```
改为：
```python
    my_peer_id = os.environ.get("COMM_AGENT_MY_PEER_ID", "ww-agent")
```

- [ ] **Step 2: 改 `main.py` docstring 默认值说明**

`agents/comm_agent/main.py:12`，把：
```python
                              (default: "agent-last-laptop")
```
改为：
```python
                              (default: "ww-agent")
```

- [ ] **Step 3: 改 `main.py` description 显示名**

`agents/comm_agent/main.py:83`，把：
```python
        description="agent-last comm-agent (A2A v0.3)",
```
改为：
```python
        description="W&W Agent comm-agent (A2A v0.3)",
```

- [ ] **Step 4: 改 `agent_card.py` provider 文案**

`agents/comm_agent/agent_card.py:35-36`，把：
```python
            "organization": "agent-last",
            "url": "https://github.com/agent-last/agent-last",
```
改为：
```python
            "organization": "W&W Agent",
            "url": "https://github.com/ww-agent/ww-agent",
```
（url 为占位；若有真实仓库地址，替换为真实地址。）

- [ ] **Step 5: 改 `a2a_protocol.py` 注释**

`agents/comm_agent/a2a_protocol.py:269`，把：
```python
    (agent-last ↔ a single Hermes/OpenClaw) is safe with one secret.
```
改为：
```python
    (W&W Agent ↔ a single Hermes/OpenClaw) is safe with one secret.
```

- [ ] **Step 6: 改 `bridge/hermes_a2a/__main__.py` 三处文案**

`bridge/hermes_a2a/__main__.py:3`，把：
```python
Run from the agent-last repo root so `agents.*` and `bridge.*` are importable:
```
改为：
```python
Run from the W&W Agent repo root so `agents.*` and `bridge.*` are importable:
```

`bridge/hermes_a2a/__main__.py:8`，把：
```python
  HERMES_A2A_HMAC          (required) shared HMAC secret with the agent-last caller
```
改为：
```python
  HERMES_A2A_HMAC          (required) shared HMAC secret with the W&W Agent caller
```

`bridge/hermes_a2a/__main__.py:38`，把：
```python
        raise SystemExit("HERMES_A2A_HMAC is required (the shared secret with agent-last)")
```
改为：
```python
        raise SystemExit("HERMES_A2A_HMAC is required (the shared secret with W&W Agent)")
```

- [ ] **Step 7: 验证导入无语法错误**

Run: `python -c "import agents.comm_agent.main, agents.comm_agent.agent_card, agents.comm_agent.a2a_protocol, bridge.hermes_a2a.__main__"`
Expected: 无输出、退出码 0（纯文案/默认值改动，不应破坏导入）。

- [ ] **Step 8: Commit**

```bash
git add agents/comm_agent/main.py agents/comm_agent/agent_card.py agents/comm_agent/a2a_protocol.py bridge/hermes_a2a/__main__.py
git commit -m "refactor: rename agent-last -> ww-agent / W&W Agent in core sources"
```

---

### Task 2: 测试硬编码改名并验证全绿

**Files:**
- Modify: `tests/test_bridge_hermes/test_e2e_bridge.py:1,26,99`
- Modify: `tests/test_bridge_hermes/conftest.py:15`
- Modify: `tests/test_comm_agent/test_agent_card.py:13,19`

- [ ] **Step 1: 改 `test_e2e_bridge.py` 客户端 peer_id（两处）**

`tests/test_bridge_hermes/test_e2e_bridge.py:26`，把：
```python
    return A2AClient(peer, secret=SECRET, my_peer_id="agent-last-laptop", transport=transport)
```
改为：
```python
    return A2AClient(peer, secret=SECRET, my_peer_id="ww-agent", transport=transport)
```

`tests/test_bridge_hermes/test_e2e_bridge.py:99`，把：
```python
        bad = A2AClient(peer, secret="WRONG", my_peer_id="agent-last-laptop",
```
改为：
```python
        bad = A2AClient(peer, secret="WRONG", my_peer_id="ww-agent",
```

- [ ] **Step 2: 改 `test_e2e_bridge.py` 顶部 docstring**

`tests/test_bridge_hermes/test_e2e_bridge.py:1`，把：
```python
"""End-to-end: agent-last's real A2AClient drives the bridge's build_app over an
```
改为：
```python
"""End-to-end: W&W Agent's real A2AClient drives the bridge's build_app over an
```

- [ ] **Step 3: 改 `conftest.py` 注释里的示例路径**

`tests/test_bridge_hermes/conftest.py:15`，把：
```python
    'D:\\Claude Code\\agent-last' — never go through shlex.split.
```
改为：
```python
    'D:\\Claude Code\\W&W Agent' — never go through shlex.split.
```

- [ ] **Step 4: 改 `test_agent_card.py` 的 name 字符串（两处）**

`tests/test_comm_agent/test_agent_card.py:13`，把：
```python
        name="agent-last-comm",
```
改为：
```python
        name="ww-agent-comm",
```

`tests/test_comm_agent/test_agent_card.py:19`，把：
```python
    assert card["name"] == "agent-last-comm"
```
改为：
```python
    assert card["name"] == "ww-agent-comm"
```

- [ ] **Step 5: 跑相关测试套件，确认全绿**

Run: `python -m pytest tests/test_bridge_hermes tests/test_comm_agent -q`
Expected: 全部 PASS，0 failed。

- [ ] **Step 6: Commit**

```bash
git add tests/test_bridge_hermes/test_e2e_bridge.py tests/test_bridge_hermes/conftest.py tests/test_comm_agent/test_agent_card.py
git commit -m "test: rename hardcoded agent-last peer ids to ww-agent"
```

---

### Task 3: 重写 `install_hermes_a2a.sh` 为交互式 + 改名

**Files:**
- Rewrite: `scripts/install_hermes_a2a.sh`

- [ ] **Step 1: 用以下完整内容覆盖 `scripts/install_hermes_a2a.sh`**

```bash
#!/usr/bin/env bash
# scripts/install_hermes_a2a.sh
# Provision the W&W Agent Hermes A2A<->ACP bridge on a remote machine so the
# W&W Agent comm-agent can delegate to / chat with a local `hermes acp` over A2A v0.3.
#
# Two ways to run:
#   1) Interactive (no flags): prompts for each value (Enter accepts the default).
#        bash install_hermes_a2a.sh
#   2) Non-interactive (flags / piped): pass values explicitly.
#        curl -sSL <raw-url> | bash -s -- \
#            --my-peer-id hermes-home \
#            --your-peer-id ww-agent \
#            --public-host home.example.com \
#            --hmac-secret "$(openssl rand -hex 32)"

set -euo pipefail

MY_PEER_ID=""
YOUR_PEER_ID=""
PUBLIC_HOST=""
HMAC_SECRET=""
HERMES_BIN="${HERMES_BIN:-hermes}"
WW_AGENT_REPO="${WW_AGENT_REPO:-https://github.com/ww-agent/ww-agent.git}"
WW_AGENT_DIR="${WW_AGENT_DIR:-$HOME/.hermes-a2a/ww-agent}"
CADDY_PORT="${CADDY_PORT:-8443}"
BRIDGE_PORT="${BRIDGE_PORT:-19444}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --my-peer-id) MY_PEER_ID="$2"; shift 2;;
    --your-peer-id) YOUR_PEER_ID="$2"; shift 2;;
    --public-host) PUBLIC_HOST="$2"; shift 2;;
    --hmac-secret) HMAC_SECRET="$2"; shift 2;;
    -h|--help)
      echo "usage: install_hermes_a2a.sh [--my-peer-id X] [--your-peer-id X] [--public-host X] [--hmac-secret X]"
      echo "  run with no flags for interactive prompts."
      exit 0;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

have_tty() { [[ -e /dev/tty ]]; }

# ask VARNAME "prompt" "default" — only prompts if the var is still empty and a TTY exists.
ask() {
  local __var="$1" __prompt="$2" __default="$3" __ans=""
  if [[ -n "${!__var}" ]]; then return 0; fi
  if ! have_tty; then return 0; fi
  if [[ -n "$__default" ]]; then
    read -r -p "$__prompt [$__default]: " __ans < /dev/tty || true
    printf -v "$__var" '%s' "${__ans:-$__default}"
  else
    read -r -p "$__prompt: " __ans < /dev/tty || true
    printf -v "$__var" '%s' "$__ans"
  fi
}

ask MY_PEER_ID   "Remote (this machine) peer id" "hermes-home"
ask YOUR_PEER_ID "Your laptop's W&W Agent peer id (must equal its COMM_AGENT_MY_PEER_ID)" "ww-agent"
ask PUBLIC_HOST  "Public host name (e.g. home.example.com)" ""
if [[ -z "$HMAC_SECRET" ]] && have_tty; then
  read -r -p "HMAC secret (blank = auto-generate): " HMAC_SECRET < /dev/tty || true
fi

# Defaults for any value still empty (non-interactive path).
MY_PEER_ID="${MY_PEER_ID:-hermes-home}"
YOUR_PEER_ID="${YOUR_PEER_ID:-ww-agent}"
if [[ -z "$HMAC_SECRET" ]]; then
  HMAC_SECRET="$(openssl rand -hex 32)"
  echo "  generated HMAC secret: $HMAC_SECRET"
fi
if [[ -z "$PUBLIC_HOST" ]]; then
  echo "ERROR: --public-host is required (no value given and no TTY to prompt)." >&2
  exit 2
fi

echo "==> [1/7] Checking Hermes ACP is available"
command -v "$HERMES_BIN" >/dev/null 2>&1 || {
  echo "ERROR: '$HERMES_BIN' not on PATH. Install Hermes (https://github.com/NousResearch/hermes-agent) or set HERMES_BIN." >&2
  exit 3
}
python3 -c "import acp" 2>/dev/null || {
  echo "  NOTE: python package 'acp' not importable. Install Hermes' ACP extra in the Hermes checkout:"
  echo "        pip install -e '.[acp]'"
  echo "  (the bridge itself does not need it, but \`hermes acp\` does)"
}

echo "==> [2/7] Fetching W&W Agent (for the reused A2A server modules)"
if [[ -d "$WW_AGENT_DIR/.git" ]]; then
  git -C "$WW_AGENT_DIR" pull --ff-only || echo "  (pull skipped)"
else
  mkdir -p "$(dirname "$WW_AGENT_DIR")"
  git clone --depth 1 "$WW_AGENT_REPO" "$WW_AGENT_DIR"
fi

echo "==> [3/7] Installing bridge python deps"
python3 -m pip install --quiet fastapi uvicorn pyjwt httpx

echo "==> [4/7] Writing bridge env file"
ENV_DIR="$HOME/.hermes-a2a"
mkdir -p "$ENV_DIR"
ENV_FILE="$ENV_DIR/bridge.env"
cat > "$ENV_FILE" <<EOF
HERMES_A2A_HMAC=$HMAC_SECRET
HERMES_A2A_MY_PEER_ID=$MY_PEER_ID
HERMES_A2A_ALLOWED_PEER=$YOUR_PEER_ID
HERMES_A2A_PORT=$BRIDGE_PORT
HERMES_A2A_PUBLIC_HOST=$PUBLIC_HOST
HERMES_A2A_PUBLIC_PORT=$CADDY_PORT
HERMES_ACP_CMD=$HERMES_BIN acp
EOF
chmod 600 "$ENV_FILE"
echo "  wrote $ENV_FILE (mode 0600)"

echo "==> [5/7] Generating Caddyfile"
CADDY_DIR="${CADDY_DIR:-/etc/caddy/Caddyfile.d}"
mkdir -p "$CADDY_DIR" 2>/dev/null || CADDY_DIR="$HOME/.caddy"
mkdir -p "$CADDY_DIR"
cat > "$CADDY_DIR/hermes-a2a.caddy" <<EOF
$PUBLIC_HOST:$CADDY_PORT {
    reverse_proxy localhost:$BRIDGE_PORT
}
EOF
echo "  wrote $CADDY_DIR/hermes-a2a.caddy"

echo "==> [6/7] Starting the bridge + reloading Caddy"
echo "  Start the bridge (loads env, runs from the W&W Agent checkout):"
echo "    cd $WW_AGENT_DIR && set -a && . $ENV_FILE && set +a && python3 -m bridge.hermes_a2a"
echo "  (For a long-running service, wrap that in a systemd unit or 'nohup ... &'.)"
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet caddy; then
  sudo systemctl reload caddy && echo "  caddy reloaded via systemctl"
else
  echo "  systemd caddy not running; start caddy manually:"
  echo "    caddy run --config $CADDY_DIR/hermes-a2a.caddy"
fi

echo "==> [7/7] Self-check hint"
echo "  After starting the bridge AND caddy, verify:"
echo "    curl -sk https://localhost:$CADDY_PORT/.well-known/agent.json"

cat <<EOF

✅ Bridge files installed.

IMPORTANT — peer id must match:
  On your laptop, COMM_AGENT_MY_PEER_ID must equal '$YOUR_PEER_ID'
  (its default is already 'ww-agent'). If it differs, the bridge will reject
  the call with 'caller peer not allowed'.

Next step on your W&W Agent machine — register this peer:
    comm.add_peer peer_id=$MY_PEER_ID \\
                  url=https://$PUBLIC_HOST:$CADDY_PORT \\
                  hmac_secret_value=$HMAC_SECRET

(Keep that HMAC secret safe — it's the only copy printed.)
EOF
```

- [ ] **Step 2: 语法检查**

Run: `bash -n scripts/install_hermes_a2a.sh`
Expected: 无输出、退出码 0。

- [ ] **Step 3: 验证残留与默认值**

Run: `grep -n 'agent-last\|AGENT_LAST' scripts/install_hermes_a2a.sh; grep -c 'ww-agent' scripts/install_hermes_a2a.sh`
Expected: 第一条无输出（无残留）；第二条计数 ≥ 1。

- [ ] **Step 4: Commit**

```bash
git add scripts/install_hermes_a2a.sh
git commit -m "feat(scripts): interactive install_hermes_a2a.sh; rename to ww-agent"
```

---

### Task 4: 重写 `install_hermes_a2a.ps1` 为交互式 + 改名

**Files:**
- Rewrite: `scripts/install_hermes_a2a.ps1`

- [ ] **Step 1: 用以下完整内容覆盖 `scripts/install_hermes_a2a.ps1`**

```powershell
# scripts/install_hermes_a2a.ps1
# Windows equivalent of install_hermes_a2a.sh.
#
# Two ways to run:
#   1) Interactive (no params): prompts for each value (Enter accepts the default).
#        .\install_hermes_a2a.ps1
#   2) Non-interactive: pass params explicitly.
#        .\install_hermes_a2a.ps1 -MyPeerId hermes-home -YourPeerId ww-agent `
#            -PublicHost home.example.com -HmacSecret <secret>

param(
    [string]$MyPeerId,
    [string]$YourPeerId,
    [string]$PublicHost,
    [string]$HmacSecret,
    [string]$HermesBin = $(if ($env:HERMES_BIN) { $env:HERMES_BIN } else { "hermes" }),
    [string]$WwAgentRepo = $(if ($env:WW_AGENT_REPO) { $env:WW_AGENT_REPO } else { "https://github.com/ww-agent/ww-agent.git" }),
    [string]$WwAgentDir = $(if ($env:WW_AGENT_DIR) { $env:WW_AGENT_DIR } else { "$env:USERPROFILE\.hermes-a2a\ww-agent" }),
    [int]$CaddyPort = 8443,
    [int]$BridgePort = 19444
)

$ErrorActionPreference = "Stop"

function Read-WithDefault($Prompt, $Default) {
    if ($Default) {
        $ans = Read-Host "$Prompt [$Default]"
        if ([string]::IsNullOrWhiteSpace($ans)) { return $Default } else { return $ans }
    } else {
        return Read-Host $Prompt
    }
}

if (-not $MyPeerId)   { $MyPeerId   = Read-WithDefault "Remote (this machine) peer id" "hermes-home" }
if (-not $YourPeerId) { $YourPeerId = Read-WithDefault "Your laptop's W&W Agent peer id (must equal its COMM_AGENT_MY_PEER_ID)" "ww-agent" }
if (-not $PublicHost) { $PublicHost = Read-WithDefault "Public host name (e.g. home.example.com)" "" }
if (-not $HmacSecret) {
    $HmacSecret = Read-Host "HMAC secret (blank = auto-generate)"
    if ([string]::IsNullOrWhiteSpace($HmacSecret)) {
        $HmacSecret = -join ((48..57)+(97..102) | Get-Random -Count 64 | ForEach-Object {[char]$_})
        Write-Host "  generated HMAC secret: $HmacSecret"
    }
}
if (-not $PublicHost) { Write-Error "PublicHost is required."; exit 2 }

Write-Host "==> [1/7] Checking Hermes ACP is available"
if (-not (Get-Command $HermesBin -ErrorAction SilentlyContinue)) {
    Write-Error "'$HermesBin' not on PATH. Install Hermes or set `$env:HERMES_BIN."
    exit 3
}
try { & python -c "import acp" 2>$null } catch {
    Write-Host "  NOTE: python package 'acp' not importable; in the Hermes checkout run: pip install -e '.[acp]'"
}

Write-Host "==> [2/7] Fetching W&W Agent (reused A2A server modules)"
if (Test-Path "$WwAgentDir\.git") {
    git -C $WwAgentDir pull --ff-only
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $WwAgentDir) | Out-Null
    git clone --depth 1 $WwAgentRepo $WwAgentDir
}

Write-Host "==> [3/7] Installing bridge python deps"
& python -m pip install --quiet fastapi uvicorn pyjwt httpx

Write-Host "==> [4/7] Writing bridge env file"
$EnvDir = "$env:USERPROFILE\.hermes-a2a"
New-Item -ItemType Directory -Force -Path $EnvDir | Out-Null
$EnvFile = "$EnvDir\bridge.env"
@"
HERMES_A2A_HMAC=$HmacSecret
HERMES_A2A_MY_PEER_ID=$MyPeerId
HERMES_A2A_ALLOWED_PEER=$YourPeerId
HERMES_A2A_PORT=$BridgePort
HERMES_A2A_PUBLIC_HOST=$PublicHost
HERMES_A2A_PUBLIC_PORT=$CaddyPort
HERMES_ACP_CMD=$HermesBin acp
"@ | Out-File -FilePath $EnvFile -Encoding utf8
$Acl = Get-Acl $EnvFile
$Acl.SetAccessRuleProtection($true, $false)
$Acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name, "Read,Write", "Allow")))
Set-Acl $EnvFile $Acl
Write-Host "  wrote $EnvFile (locked to current user)"

Write-Host "==> [5/7] Generating Caddyfile"
$CaddyDir = if ($env:CADDY_DIR) { $env:CADDY_DIR } else { "$env:USERPROFILE\.caddy" }
New-Item -ItemType Directory -Force -Path $CaddyDir | Out-Null
@"
${PublicHost}:${CaddyPort} {
    reverse_proxy localhost:$BridgePort
}
"@ | Out-File -FilePath "$CaddyDir\hermes-a2a.caddy" -Encoding utf8
Write-Host "  wrote $CaddyDir\hermes-a2a.caddy"

Write-Host "==> [6/7] Start the bridge + reload Caddy"
Write-Host "  Start the bridge from the W&W Agent checkout:"
Write-Host "    cd $WwAgentDir; Get-Content $EnvFile | ForEach-Object { if (`$_ -match '^(.+?)=(.*)$') { [Environment]::SetEnvironmentVariable(`$Matches[1], `$Matches[2]) } }; python -m bridge.hermes_a2a"
if (Get-Service -Name "caddy" -ErrorAction SilentlyContinue) {
    Restart-Service -Name "caddy"; Write-Host "  caddy service restarted"
} else {
    Write-Host "  caddy service not found; start manually: caddy run --config $CaddyDir\hermes-a2a.caddy"
}

Write-Host "==> [7/7] Self-check hint"
Write-Host "  After starting bridge + caddy: curl -sk https://localhost:$CaddyPort/.well-known/agent.json"

Write-Host ""
Write-Host "[OK] Bridge files installed."
Write-Host ""
Write-Host "IMPORTANT - peer id must match:"
Write-Host "  On your laptop, COMM_AGENT_MY_PEER_ID must equal '$YourPeerId'"
Write-Host "  (its default is already 'ww-agent'). Otherwise the bridge rejects"
Write-Host "  the call with 'caller peer not allowed'."
Write-Host ""
Write-Host "Next step on your W&W Agent machine - register this peer:"
Write-Host "    comm.add_peer peer_id=$MyPeerId url=https://${PublicHost}:${CaddyPort} hmac_secret_value=$HmacSecret"
Write-Host "(Keep that HMAC secret safe - it's the only copy printed.)"
```

- [ ] **Step 2: 语法检查（PowerShell AST 解析，不执行脚本）**

Run（PowerShell）：
```powershell
$errs=$null; [void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path .\scripts\install_hermes_a2a.ps1).Path,[ref]$null,[ref]$errs); if ($errs) { $errs; exit 1 } else { "OK" }
```
Expected: 输出 `OK`，无解析错误。

- [ ] **Step 3: 验证残留**

Run: `grep -n 'agent-last\|AGENT_LAST\|AgentLast' scripts/install_hermes_a2a.ps1`
Expected: 无输出。

- [ ] **Step 4: Commit**

```bash
git add scripts/install_hermes_a2a.ps1
git commit -m "feat(scripts): interactive install_hermes_a2a.ps1; rename to ww-agent"
```

---

### Task 5: 重写 `install_openclaw_a2a.sh` 为交互式 + 改名

**Files:**
- Rewrite: `scripts/install_openclaw_a2a.sh`

- [ ] **Step 1: 用以下完整内容覆盖 `scripts/install_openclaw_a2a.sh`**

```bash
#!/usr/bin/env bash
# scripts/install_openclaw_a2a.sh
# Install the openclaw-a2a plugin on a remote machine so the W&W Agent
# comm-agent can talk to it over Google A2A v0.3.
#
# Two ways to run:
#   1) Interactive (no flags): prompts for each value (Enter accepts the default).
#        bash install_openclaw_a2a.sh
#   2) Non-interactive (flags / piped):
#        curl -sSL <raw-url> | bash -s -- \
#            --my-peer-id openclaw-home \
#            --your-peer-id ww-agent \
#            --public-host home.example.com \
#            --hmac-secret "$(openssl rand -hex 32)"

set -euo pipefail

MY_PEER_ID=""
YOUR_PEER_ID=""
PUBLIC_HOST=""
HMAC_SECRET=""
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
A2A_PLUGIN_VERSION="${A2A_PLUGIN_VERSION:-v0.3.0}"
CADDY_PORT="${CADDY_PORT:-8443}"
OPENCLAW_A2A_PORT="${OPENCLAW_A2A_PORT:-19443}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --my-peer-id) MY_PEER_ID="$2"; shift 2;;
    --your-peer-id) YOUR_PEER_ID="$2"; shift 2;;
    --public-host) PUBLIC_HOST="$2"; shift 2;;
    --hmac-secret) HMAC_SECRET="$2"; shift 2;;
    -h|--help)
      echo "usage: install_openclaw_a2a.sh [--my-peer-id X] [--your-peer-id X] [--public-host X] [--hmac-secret X]"
      echo "  run with no flags for interactive prompts."
      exit 0;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

have_tty() { [[ -e /dev/tty ]]; }

ask() {
  local __var="$1" __prompt="$2" __default="$3" __ans=""
  if [[ -n "${!__var}" ]]; then return 0; fi
  if ! have_tty; then return 0; fi
  if [[ -n "$__default" ]]; then
    read -r -p "$__prompt [$__default]: " __ans < /dev/tty || true
    printf -v "$__var" '%s' "${__ans:-$__default}"
  else
    read -r -p "$__prompt: " __ans < /dev/tty || true
    printf -v "$__var" '%s' "$__ans"
  fi
}

ask MY_PEER_ID   "Remote (this machine) peer id" "openclaw-home"
ask YOUR_PEER_ID "Your laptop's W&W Agent peer id (must equal its COMM_AGENT_MY_PEER_ID)" "ww-agent"
ask PUBLIC_HOST  "Public host name (e.g. home.example.com)" ""
if [[ -z "$HMAC_SECRET" ]] && have_tty; then
  read -r -p "HMAC secret (blank = auto-generate): " HMAC_SECRET < /dev/tty || true
fi

MY_PEER_ID="${MY_PEER_ID:-openclaw-home}"
YOUR_PEER_ID="${YOUR_PEER_ID:-ww-agent}"
if [[ -z "$HMAC_SECRET" ]]; then
  HMAC_SECRET="$(openssl rand -hex 32)"
  echo "  generated HMAC secret: $HMAC_SECRET"
fi
if [[ -z "$PUBLIC_HOST" ]]; then
  echo "ERROR: --public-host is required (no value given and no TTY to prompt)." >&2
  exit 2
fi

echo "==> [1/7] Checking OpenClaw is installed"
command -v "$OPENCLAW_BIN" >/dev/null 2>&1 || {
  echo "ERROR: '$OPENCLAW_BIN' not on PATH. Install OpenClaw first (https://github.com/openclaw/openclaw) or export OPENCLAW_BIN." >&2
  exit 3
}

echo "==> [2/7] Installing openclaw-a2a plugin @ $A2A_PLUGIN_VERSION"
"$OPENCLAW_BIN" skill install "marketclaw-tech/openclaw-a2a@$A2A_PLUGIN_VERSION"

echo "==> [3/7] Writing OpenClaw A2A config"
OPENCLAW_CONFIG_DIR="$($OPENCLAW_BIN config-dir 2>/dev/null || echo "$HOME/.openclaw")"
mkdir -p "$OPENCLAW_CONFIG_DIR"
cat > "$OPENCLAW_CONFIG_DIR/a2a.yaml" <<EOF
a2a:
  my_peer_id: "$MY_PEER_ID"
  listen_port: $OPENCLAW_A2A_PORT
  hmac_secret_env: A2A_HMAC_SECRET
  allowed_peers:
    - peer_id: "$YOUR_PEER_ID"
      hmac_secret_env: A2A_HMAC_SECRET
EOF
echo "  wrote $OPENCLAW_CONFIG_DIR/a2a.yaml"

echo "==> [4/7] Persisting HMAC secret to env"
ENV_FILE="$OPENCLAW_CONFIG_DIR/a2a.env"
echo "A2A_HMAC_SECRET=$HMAC_SECRET" > "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "  wrote $ENV_FILE (mode 0600)"

echo "==> [5/7] Generating Caddyfile"
CADDY_DIR="${CADDY_DIR:-/etc/caddy/Caddyfile.d}"
mkdir -p "$CADDY_DIR" 2>/dev/null || CADDY_DIR="$HOME/.caddy"
mkdir -p "$CADDY_DIR"
cat > "$CADDY_DIR/openclaw-a2a.caddy" <<EOF
$PUBLIC_HOST:$CADDY_PORT {
    reverse_proxy localhost:$OPENCLAW_A2A_PORT
}
EOF
echo "  wrote $CADDY_DIR/openclaw-a2a.caddy"

echo "==> [6/7] Reloading Caddy"
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet caddy; then
  sudo systemctl reload caddy
  echo "  caddy reloaded via systemctl"
else
  echo "  systemd caddy not running; you'll need to start caddy manually:"
  echo "    caddy run --config $CADDY_DIR/openclaw-a2a.caddy"
fi

echo "==> [7/7] Self-check"
sleep 2
if curl -sk --max-time 5 "https://localhost:$CADDY_PORT/.well-known/agent.json" >/dev/null; then
  echo "  agent card served OK"
else
  echo "  WARNING: agent card not yet reachable on https://localhost:$CADDY_PORT/"
fi

cat <<EOF

✅ Install complete.

IMPORTANT — peer id must match:
  On your laptop, COMM_AGENT_MY_PEER_ID must equal '$YOUR_PEER_ID'
  (its default is already 'ww-agent'), or calls fail with 'caller peer not allowed'.

Next step on your laptop:
  In the comm-agent REPL, register this peer:
    comm.add_peer peer_id=$MY_PEER_ID \\
                  url=https://$PUBLIC_HOST:$CADDY_PORT \\
                  hmac_secret_value=$HMAC_SECRET

(Keep that HMAC secret safe — it's the only copy printed.)
EOF
```

- [ ] **Step 2: 语法检查**

Run: `bash -n scripts/install_openclaw_a2a.sh`
Expected: 无输出、退出码 0。

- [ ] **Step 3: 验证残留**

Run: `grep -n 'agent-last' scripts/install_openclaw_a2a.sh`
Expected: 无输出。

- [ ] **Step 4: Commit**

```bash
git add scripts/install_openclaw_a2a.sh
git commit -m "feat(scripts): interactive install_openclaw_a2a.sh; rename to ww-agent"
```

---

### Task 6: 重写 `install_openclaw_a2a.ps1` 为交互式 + 改名

**Files:**
- Rewrite: `scripts/install_openclaw_a2a.ps1`

- [ ] **Step 1: 用以下完整内容覆盖 `scripts/install_openclaw_a2a.ps1`**

```powershell
# scripts/install_openclaw_a2a.ps1
# Windows equivalent of install_openclaw_a2a.sh.
#
# Two ways to run:
#   1) Interactive (no params): prompts for each value (Enter accepts the default).
#        .\install_openclaw_a2a.ps1
#   2) Non-interactive: pass params explicitly.
#        .\install_openclaw_a2a.ps1 -MyPeerId openclaw-home -YourPeerId ww-agent `
#            -PublicHost home.example.com -HmacSecret <secret>

param(
    [string]$MyPeerId,
    [string]$YourPeerId,
    [string]$PublicHost,
    [string]$HmacSecret,
    [string]$OpenclawBin = $(if ($env:OPENCLAW_BIN) { $env:OPENCLAW_BIN } else { "openclaw" }),
    [string]$A2APluginVersion = "v0.3.0",
    [int]$CaddyPort = 8443,
    [int]$OpenclawA2APort = 19443
)

$ErrorActionPreference = "Stop"

function Read-WithDefault($Prompt, $Default) {
    if ($Default) {
        $ans = Read-Host "$Prompt [$Default]"
        if ([string]::IsNullOrWhiteSpace($ans)) { return $Default } else { return $ans }
    } else {
        return Read-Host $Prompt
    }
}

if (-not $MyPeerId)   { $MyPeerId   = Read-WithDefault "Remote (this machine) peer id" "openclaw-home" }
if (-not $YourPeerId) { $YourPeerId = Read-WithDefault "Your laptop's W&W Agent peer id (must equal its COMM_AGENT_MY_PEER_ID)" "ww-agent" }
if (-not $PublicHost) { $PublicHost = Read-WithDefault "Public host name (e.g. home.example.com)" "" }
if (-not $HmacSecret) {
    $HmacSecret = Read-Host "HMAC secret (blank = auto-generate)"
    if ([string]::IsNullOrWhiteSpace($HmacSecret)) {
        $HmacSecret = -join ((48..57)+(97..102) | Get-Random -Count 64 | ForEach-Object {[char]$_})
        Write-Host "  generated HMAC secret: $HmacSecret"
    }
}
if (-not $PublicHost) { Write-Error "PublicHost is required."; exit 2 }

Write-Host "==> [1/7] Checking OpenClaw is installed"
if (-not (Get-Command $OpenclawBin -ErrorAction SilentlyContinue)) {
    Write-Error "'$OpenclawBin' not on PATH. Install OpenClaw or set `$env:OPENCLAW_BIN."
    exit 3
}

Write-Host "==> [2/7] Installing openclaw-a2a plugin @ $A2APluginVersion"
& $OpenclawBin skill install "marketclaw-tech/openclaw-a2a@$A2APluginVersion"

Write-Host "==> [3/7] Writing OpenClaw A2A config"
$OpenclawConfigDir = & $OpenclawBin config-dir 2>$null
if (-not $OpenclawConfigDir) { $OpenclawConfigDir = "$env:USERPROFILE\.openclaw" }
New-Item -ItemType Directory -Force -Path $OpenclawConfigDir | Out-Null
$ConfigYaml = @"
a2a:
  my_peer_id: "$MyPeerId"
  listen_port: $OpenclawA2APort
  hmac_secret_env: A2A_HMAC_SECRET
  allowed_peers:
    - peer_id: "$YourPeerId"
      hmac_secret_env: A2A_HMAC_SECRET
"@
$ConfigYaml | Out-File -FilePath "$OpenclawConfigDir\a2a.yaml" -Encoding utf8
Write-Host "  wrote $OpenclawConfigDir\a2a.yaml"

Write-Host "==> [4/7] Persisting HMAC secret to env file"
$EnvFile = "$OpenclawConfigDir\a2a.env"
"A2A_HMAC_SECRET=$HmacSecret" | Out-File -FilePath $EnvFile -Encoding utf8
$Acl = Get-Acl $EnvFile
$Acl.SetAccessRuleProtection($true, $false)
$Acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
    "Read,Write", "Allow"
)))
Set-Acl $EnvFile $Acl
Write-Host "  wrote $EnvFile (locked to current user)"

Write-Host "==> [5/7] Generating Caddyfile"
$CaddyDir = if ($env:CADDY_DIR) { $env:CADDY_DIR } else { "$env:USERPROFILE\.caddy" }
New-Item -ItemType Directory -Force -Path $CaddyDir | Out-Null
$Caddyfile = @"
${PublicHost}:${CaddyPort} {
    reverse_proxy localhost:$OpenclawA2APort
}
"@
$Caddyfile | Out-File -FilePath "$CaddyDir\openclaw-a2a.caddy" -Encoding utf8
Write-Host "  wrote $CaddyDir\openclaw-a2a.caddy"

Write-Host "==> [6/7] Caddy reload"
if (Get-Service -Name "caddy" -ErrorAction SilentlyContinue) {
    Restart-Service -Name "caddy"
    Write-Host "  caddy service restarted"
} else {
    Write-Host "  caddy service not found; start manually:"
    Write-Host "    caddy run --config $CaddyDir\openclaw-a2a.caddy"
}

Write-Host "==> [7/7] Self-check"
Start-Sleep -Seconds 2
try {
    Invoke-WebRequest -Uri "https://localhost:$CaddyPort/.well-known/agent.json" `
        -SkipCertificateCheck -TimeoutSec 5 -UseBasicParsing | Out-Null
    Write-Host "  agent card served OK"
} catch {
    Write-Host "  WARNING: agent card not yet reachable on https://localhost:$CaddyPort/"
}

Write-Host ""
Write-Host "[OK] Install complete."
Write-Host ""
Write-Host "IMPORTANT - peer id must match:"
Write-Host "  On your laptop, COMM_AGENT_MY_PEER_ID must equal '$YourPeerId'"
Write-Host "  (its default is already 'ww-agent'), or calls fail with 'caller peer not allowed'."
Write-Host ""
Write-Host "Next step on your laptop:"
Write-Host "  In the comm-agent REPL, register this peer:"
Write-Host "    comm.add_peer peer_id=$MyPeerId url=https://${PublicHost}:${CaddyPort} hmac_secret_value=$HmacSecret"
Write-Host ""
Write-Host "(Keep that HMAC secret safe - it's the only copy printed.)"
```

- [ ] **Step 2: 语法检查**

Run（PowerShell）：
```powershell
$errs=$null; [void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path .\scripts\install_openclaw_a2a.ps1).Path,[ref]$null,[ref]$errs); if ($errs) { $errs; exit 1 } else { "OK" }
```
Expected: 输出 `OK`。

- [ ] **Step 3: 验证残留**

Run: `grep -n 'agent-last' scripts/install_openclaw_a2a.ps1`
Expected: 无输出。

- [ ] **Step 4: Commit**

```bash
git add scripts/install_openclaw_a2a.ps1
git commit -m "feat(scripts): interactive install_openclaw_a2a.ps1; rename to ww-agent"
```

---

### Task 7: README 改名 + 全仓残留校验

**Files:**
- Modify: `agents/comm_agent/README.md`（多处，见下）

- [ ] **Step 1: 替换 README 中所有 `agent-last-laptop` 为 `ww-agent`**

逐处替换 `agents/comm_agent/README.md`（行号供定位，文本以实际为准）：
- 行 185、195、276：示例命令里 `--your-peer-id agent-last-laptop` / `-YourPeerId agent-last-laptop` → `ww-agent`
- 行 203：表格说明 `（默认 agent-last-laptop）` → `（默认 ww-agent）`
- 行 220：白名单示例注释 `peer_id: "agent-last-laptop"` → `"ww-agent"`
- 行 375：`COMM_AGENT_MY_PEER_ID` 默认值列 `agent-last-laptop` → `ww-agent`
- 行 404：JSON 示例 `"peer_id": "agent-last-laptop"` → `"ww-agent"`

- [ ] **Step 2: 替换 README 中剩余的 `agent-last` 显示名**

- 行 266：`agent-last 侧零改动` → `W&W Agent 侧零改动`
- 行 281：`脚本会：拉 agent-last（…）` → `脚本会：拉 W&W Agent（…）`

- [ ] **Step 3: 残留校验（核心验收）**

Run: `grep -rn 'agent-last' --include='*.py' --include='*.md' --include='*.sh' --include='*.ps1' agents bridge scripts tests`
Expected: 无输出。若有命中，回到对应文件清除。
（注意：刻意只扫 `agents bridge scripts tests` 四个目录——`docs/superpowers/` 下的规格与本计划文档为记录历史会引用旧名 `agent-last`，不应纳入校验。）

- [ ] **Step 4: 跑全量测试套件**

Run: `python -m pytest -q`
Expected: 全绿，0 failed。

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/README.md
git commit -m "docs: rename agent-last -> ww-agent / W&W Agent in comm-agent README"
```

---

## 收尾

全部 Task 完成后：
- `grep -rn 'agent-last' agents bridge scripts tests` 零命中（`docs/superpowers/` 规格除外）。
- `python -m pytest -q` 全绿。
- 4 个脚本：不带参数交互、带参数沿用旧行为；默认 `your-peer-id = ww-agent` 与本机默认 `COMM_AGENT_MY_PEER_ID = ww-agent` 对齐，复现不再 `caller peer not allowed`。
- 可选：用 `superpowers:finishing-a-development-branch` 决定合并/PR。
