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

function New-HmacSecret {
    # 32 random bytes -> 64 hex chars (256-bit), matches `openssl rand -hex 32`.
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
}

if (-not $MyPeerId)   { $MyPeerId   = Read-WithDefault "Remote (this machine) peer id" "hermes-home" }
if (-not $YourPeerId) { $YourPeerId = Read-WithDefault "Your laptop's W&W Agent peer id (must equal its COMM_AGENT_MY_PEER_ID)" "ww-agent" }
while (-not $PublicHost) { $PublicHost = Read-Host "Public host name (e.g. home.example.com)" }
if (-not $HmacSecret) {
    $HmacSecret = Read-Host "HMAC secret (blank = auto-generate)"
    if ([string]::IsNullOrWhiteSpace($HmacSecret)) {
        $HmacSecret = New-HmacSecret
        Write-Host "  generated HMAC secret: $HmacSecret"
    }
}

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
