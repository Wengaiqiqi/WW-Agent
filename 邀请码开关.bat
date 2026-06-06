@echo off
REM ===========================================================================
REM  W&W Agent  Web invitation-code switch (persistent, config-only).
REM  This .bat header is pure ASCII on purpose: cmd.exe cannot reliably parse
REM  non-ASCII batch source, so the real (Chinese) UI lives in the PowerShell
REM  section below the #PSSTART# marker and is executed by powershell.exe.
REM  It only toggles the persistent on-disk signup-code gate that the web
REM  server reads live; it does NOT launch the server. Start the server with
REM  start_web.bat as usual.
REM ===========================================================================
set "SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText($env:SELF,[Text.Encoding]::UTF8); iex $s.Substring($s.LastIndexOf('#PSSTART#')+9)"
goto :eof

#PSSTART#
# ===== PowerShell from here (UTF-8) =====================================
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::GetEncoding(936) } catch {}

# 邀请码存到磁盘文件 <project>\.langchain-agent\web\signup_code。
# 服务器每次注册都现读这个文件，所以开/关“立即生效”——哪怕服务正开着也认，
# 不用重启。文件路径相对本 .bat 所在目录(= start_web.bat 启动时的工作目录)。
$Root     = Split-Path -Parent $env:SELF
$CodeFile = Join-Path $Root '.langchain-agent\web\signup_code'

# 旧机制曾把邀请码写成“用户级环境变量”。它会盖过文件(环境变量优先级更高)，
# 所以每次都顺手清掉，保证文件是唯一的真相来源。
$LegacyVar = 'WEB_SIGNUP_CODE'
[Environment]::SetEnvironmentVariable($LegacyVar, $null, 'User')

$current = ''
if (Test-Path -LiteralPath $CodeFile) {
    $current = (Get-Content -LiteralPath $CodeFile -Raw -Encoding UTF8).Trim()
}

Write-Host ''
Write-Host '============================================'
Write-Host '   W&W Agent  Web 邀请码开关'
if ([string]::IsNullOrWhiteSpace($current)) {
    Write-Host '   当前状态: [已关闭] 开放注册'
} else {
    Write-Host "   当前状态: [已开启] 邀请码 = $current"
}
Write-Host '============================================'
Write-Host '  [1] 开启邀请码 (输入一个码，立即生效)'
Write-Host '  [2] 关闭邀请码 (删除设置 = 开放注册)'
Write-Host '  [0] 退出'
Write-Host ''
$choice = Read-Host '请选择 [1/2/0]'

if ($choice -eq '1') {
    $code = Read-Host '请输入要使用的邀请码'
    if ([string]::IsNullOrWhiteSpace($code)) {
        Write-Host '邀请码不能为空，已取消。'
    } else {
        $dir = Split-Path -Parent $CodeFile
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        # 不带 BOM 写入，避免服务端读出多余字符。
        [IO.File]::WriteAllText($CodeFile, $code.Trim(), (New-Object Text.UTF8Encoding($false)))
        Write-Host ''
        Write-Host '[开] 邀请码已保存到磁盘，立即生效。'
        Write-Host "       邀请码: $($code.Trim())"
        Write-Host '       把这串发给信任的用户，注册时填写。'
        Write-Host '       服务正开着也无需重启，下一次注册就会校验。'
    }
} elseif ($choice -eq '2') {
    if (Test-Path -LiteralPath $CodeFile) {
        Remove-Item -LiteralPath $CodeFile -Force
    }
    Write-Host ''
    Write-Host '[关] 已删除邀请码设置 = 开放注册，立即生效。'
    Write-Host ''
    Write-Host '提示: start_web.bat 默认绑 127.0.0.1 (仅本机)，开放注册是安全的。'
    Write-Host '      若改成对外暴露 (0.0.0.0 / 局域网)，server 会拒绝“开放注册 + 对外暴露”的'
    Write-Host '      组合并拒绝启动——那种情况请改回 [1] 开启邀请码。'
} elseif ($choice -eq '0') {
    Write-Host '已退出。'
} else {
    Write-Host '无效选择。'
}

Write-Host ''
Read-Host '按回车键退出'
