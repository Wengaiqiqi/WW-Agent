@echo off
REM ===========================================================================
REM  W&W Agent  Web invitation-code switch (persistent, config-only).
REM  This .bat header is pure ASCII on purpose: cmd.exe cannot reliably parse
REM  non-ASCII batch source, so the real (Chinese) UI lives in the PowerShell
REM  section below the #PSSTART# marker and is executed by powershell.exe.
REM  It only toggles a persistent user-level WEB_SIGNUP_CODE env var; it does
REM  NOT launch the server. Start the server with start_web.bat as usual.
REM ===========================================================================
set "SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText($env:SELF,[Text.Encoding]::UTF8); iex $s.Substring($s.LastIndexOf('#PSSTART#')+9)"
goto :eof

#PSSTART#
# ===== PowerShell from here (UTF-8) =====================================
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::GetEncoding(936) } catch {}

# 把邀请码存成“用户级环境变量”(写进 HKCU\Environment)，永久保存、跨重启。
# 之后任何新启动的进程(含 start_web.bat)都会自动读到它。设完即退出，不挂窗口。
$VarName = 'WEB_SIGNUP_CODE'
$current = [Environment]::GetEnvironmentVariable($VarName, 'User')

Write-Host ''
Write-Host '============================================'
Write-Host '   W&W Agent  Web 邀请码开关'
if ([string]::IsNullOrWhiteSpace($current)) {
    Write-Host '   当前状态: [已关闭] 开放注册'
} else {
    Write-Host "   当前状态: [已开启] 邀请码 = $current"
}
Write-Host '============================================'
Write-Host '  [1] 开启邀请码 (输入一个码，永久保存)'
Write-Host '  [2] 关闭邀请码 (删除设置 = 开放注册)'
Write-Host '  [0] 退出'
Write-Host ''
$choice = Read-Host '请选择 [1/2/0]'

if ($choice -eq '1') {
    $code = Read-Host '请输入要使用的邀请码'
    if ([string]::IsNullOrWhiteSpace($code)) {
        Write-Host '邀请码不能为空，已取消。'
    } else {
        [Environment]::SetEnvironmentVariable($VarName, $code, 'User')
        Write-Host ''
        Write-Host '[开] 邀请码已永久保存 (用户级环境变量)。'
        Write-Host "       邀请码: $code"
        Write-Host '       把这串发给信任的用户，注册时填写。'
        Write-Host ''
        Write-Host '提示: 下次双击 start_web.bat 启动即生效。'
        Write-Host '      若服务/命令行窗口当前已开着，需要关掉重开一次才认。'
    }
} elseif ($choice -eq '2') {
    [Environment]::SetEnvironmentVariable($VarName, $null, 'User')
    Write-Host ''
    Write-Host '[关] 已删除邀请码设置 = 开放注册。'
    Write-Host ''
    Write-Host '提示: 下次启动为开放注册。start_web.bat 默认绑 127.0.0.1 (仅本机)，开放注册是安全的。'
    Write-Host '      若改成对外暴露 (0.0.0.0 / 局域网)，server 会拒绝“开放注册 + 对外暴露”的'
    Write-Host '      组合并拒绝启动——那种情况请改回 [1] 开启邀请码。'
} elseif ($choice -eq '0') {
    Write-Host '已退出。'
} else {
    Write-Host '无效选择。'
}

Write-Host ''
Read-Host '按回车键退出'
