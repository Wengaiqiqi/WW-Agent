# Start the Agent Web UI (dev) and open it in your browser.
#
#   .\start_web.ps1                    # http://127.0.0.1:8080, opens the browser
#   .\start_web.ps1 -Port 9000         # different port
#   .\start_web.ps1 -BindHost 0.0.0.0  # expose on the LAN (-Host also works)
#   .\start_web.ps1 -NoBrowser         # don't auto-open the browser
#
# Dev defaults: local http (Secure cookie off) and — if WEB_AUTH_SECRET is
# unset — an ephemeral per-process auth secret (tokens reset on restart, you'll
# see a warning). For non-dev use, set WEB_AUTH_SECRET / WEB_SIGNUP_CODE in your
# environment first. Ctrl+C stops the server.

[CmdletBinding()]
param(
    [Alias("Host")]               # keep the documented -Host usage working
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8080,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Dev default: only relax the Secure cookie attribute when bound to loopback.
# Non-loopback (LAN / 0.0.0.0) means the session JWT would otherwise traverse
# the wire in cleartext; force the operator to opt in explicitly via env so
# `start_web.ps1 -BindHost 0.0.0.0` cannot silently downgrade to insecure
# cookies.
$loopbackBinds = @("127.0.0.1", "localhost", "::1")
$isLoopback = $loopbackBinds -contains $BindHost
if (-not $env:WEB_COOKIE_SECURE) {
    if ($isLoopback) {
        $env:WEB_COOKIE_SECURE = "0"
    } else {
        Write-Host "Refusing to start: BindHost '$BindHost' is non-loopback but WEB_COOKIE_SECURE is unset." -ForegroundColor Red
        Write-Host "Set up TLS (Caddy) and run with WEB_COOKIE_SECURE=1, or set WEB_COOKIE_SECURE=0 explicitly to accept the risk." -ForegroundColor Yellow
        exit 1
    }
}

# 0.0.0.0 isn't browsable — point the browser at localhost in that case.
$urlHost = if ($BindHost -eq "0.0.0.0") { "127.0.0.1" } else { $BindHost }
$url = "http://${urlHost}:$Port/"

if (-not $NoBrowser) {
    # Wait (in the background) until the port accepts connections, then open the
    # browser. uvicorn keeps the foreground so Ctrl+C stops it cleanly.
    Start-Job -Name "web-open" -ArgumentList $urlHost, $Port, $url -ScriptBlock {
        param($h, $p, $u)
        for ($i = 0; $i -lt 100; $i++) {
            $client = New-Object Net.Sockets.TcpClient
            try { $client.Connect($h, $p); Start-Process $u; break }
            catch { Start-Sleep -Milliseconds 200 }
            finally { $client.Dispose() }
        }
    } | Out-Null
}

Write-Host "Agent Web UI -> $url  (Ctrl+C to stop)" -ForegroundColor Green
try {
    python -m web --host $BindHost --port $Port
}
finally {
    Get-Job -Name "web-open" -ErrorAction SilentlyContinue | Remove-Job -Force
}
