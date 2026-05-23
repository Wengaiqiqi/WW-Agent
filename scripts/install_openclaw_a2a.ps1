# scripts/install_openclaw_a2a.ps1
# Windows equivalent of install_openclaw_a2a.sh.
#
# Usage:
#   iex "& { $(iwr -useb <raw-url>) } -MyPeerId openclaw-home -YourPeerId agent-last-laptop -PublicHost home.example.com -HmacSecret (-join ((48..57) + (97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_}))"

param(
    [Parameter(Mandatory=$true)][string]$MyPeerId,
    [Parameter(Mandatory=$true)][string]$YourPeerId,
    [Parameter(Mandatory=$true)][string]$PublicHost,
    [Parameter(Mandatory=$true)][string]$HmacSecret,
    [string]$OpenclawBin = $(if ($env:OPENCLAW_BIN) { $env:OPENCLAW_BIN } else { "openclaw" }),
    [string]$A2APluginVersion = "v0.3.0",
    [int]$CaddyPort = 8443,
    [int]$OpenclawA2APort = 19443
)

$ErrorActionPreference = "Stop"

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
# Lock to current user
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
Write-Host "Next step on your laptop:"
Write-Host "  In the comm-agent REPL, register this peer:"
Write-Host "    comm.add_peer peer_id=$MyPeerId \"
Write-Host "                  url=https://${PublicHost}:${CaddyPort} \"
Write-Host "                  hmac_secret_value=$HmacSecret"
Write-Host ""
Write-Host "(Keep that HMAC secret safe — it's the only copy printed.)"
