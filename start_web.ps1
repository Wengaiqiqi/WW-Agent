# Start the web UI frontend (dev).
#
#   .\start_web.ps1                 # http://127.0.0.1:8080
#   .\start_web.ps1 -Port 9000
#   .\start_web.ps1 -Host 0.0.0.0   # expose on LAN
#
# Set WEB_AUTH_SECRET / WEB_SIGNUP_CODE in your environment for non-dev use.

param(
    [string]$Host = "127.0.0.1",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Dev defaults: local http (no Secure cookie), ephemeral auth secret if unset.
if (-not $env:WEB_COOKIE_SECURE) { $env:WEB_COOKIE_SECURE = "0" }

python -m web --host $Host --port $Port
