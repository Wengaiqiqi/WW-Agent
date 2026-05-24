# scripts/install_hermes_a2a.ps1
# Windows equivalent of install_hermes_a2a.sh.
#
# Usage:
#   $secret = -join ((48..57)+(97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})
#   iex "& { $(iwr -useb <raw-url>) } -MyPeerId hermes-home -YourPeerId agent-last-laptop -PublicHost home.example.com -HmacSecret $secret"

param(
    [Parameter(Mandatory=$true)][string]$MyPeerId,
    [Parameter(Mandatory=$true)][string]$YourPeerId,
    [Parameter(Mandatory=$true)][string]$PublicHost,
    [Parameter(Mandatory=$true)][string]$HmacSecret,
    [string]$HermesBin = $(if ($env:HERMES_BIN) { $env:HERMES_BIN } else { "hermes" }),
    [string]$AgentLastRepo = $(if ($env:AGENT_LAST_REPO) { $env:AGENT_LAST_REPO } else { "https://github.com/<your-repo>/agent-last.git" }),
    [string]$AgentLastDir = $(if ($env:AGENT_LAST_DIR) { $env:AGENT_LAST_DIR } else { "$env:USERPROFILE\.hermes-a2a\agent-last" }),
    [int]$CaddyPort = 8443,
    [int]$BridgePort = 19444
)

$ErrorActionPreference = "Stop"

Write-Host "==> [1/7] Checking Hermes ACP is available"
if (-not (Get-Command $HermesBin -ErrorAction SilentlyContinue)) {
    Write-Error "'$HermesBin' not on PATH. Install Hermes or set `$env:HERMES_BIN."
    exit 3
}
try { & python -c "import acp" 2>$null } catch {
    Write-Host "  NOTE: python package 'acp' not importable; in the Hermes checkout run: pip install -e '.[acp]'"
}

Write-Host "==> [2/7] Fetching agent-last (reused A2A server modules)"
if (Test-Path "$AgentLastDir\.git") {
    git -C $AgentLastDir pull --ff-only
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $AgentLastDir) | Out-Null
    git clone --depth 1 $AgentLastRepo $AgentLastDir
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
Write-Host "  Start the bridge from the agent-last checkout:"
Write-Host "    cd $AgentLastDir; Get-Content $EnvFile | ForEach-Object { if (`$_ -match '^(.+?)=(.*)$') { [Environment]::SetEnvironmentVariable(`$Matches[1], `$Matches[2]) } }; python -m bridge.hermes_a2a"
if (Get-Service -Name "caddy" -ErrorAction SilentlyContinue) {
    Restart-Service -Name "caddy"; Write-Host "  caddy service restarted"
} else {
    Write-Host "  caddy service not found; start manually: caddy run --config $CaddyDir\hermes-a2a.caddy"
}

Write-Host "==> [7/7] Self-check hint"
Write-Host "  After starting bridge + caddy: curl -sk https://localhost:$CaddyPort/.well-known/agent.json"

Write-Host ""
Write-Host "[OK] Bridge files installed."
Write-Host "Next step on your agent-last machine — register this peer:"
Write-Host "    comm.add_peer peer_id=$MyPeerId url=https://${PublicHost}:${CaddyPort} hmac_secret_value=$HmacSecret"
Write-Host "(Keep that HMAC secret safe — it's the only copy printed.)"
