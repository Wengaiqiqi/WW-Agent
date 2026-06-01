@echo off
setlocal

rem Start the Agent Web UI (dev) and open it in your browser.
rem
rem   start_web.bat                ::  http://127.0.0.1:8080  (opens browser)
rem   start_web.bat 9000           ::  custom port
rem   start_web.bat 9000 0.0.0.0   ::  custom port + bind host (expose on LAN)
rem   set WEB_NO_BROWSER=1 ^& start_web.bat   ::  don't open the browser
rem
rem Dev defaults: plain http (Secure cookie off) and, if WEB_AUTH_SECRET is
rem unset, an ephemeral auth secret (tokens reset on restart, you'll see a
rem warning). Set WEB_AUTH_SECRET / WEB_SIGNUP_CODE for non-dev use.
rem Ctrl+C stops the server.

cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8080"
set "BIND=%~2"
if "%BIND%"=="" set "BIND=127.0.0.1"

rem 0.0.0.0 isn't browsable -> point the browser at localhost instead.
set "URLHOST=%BIND%"
if "%URLHOST%"=="0.0.0.0" set "URLHOST=127.0.0.1"
set "URL=http://%URLHOST%:%PORT%/"

rem Dev default: only relax Secure cookie when bound to loopback. Otherwise
rem refuse to start so the session JWT is not silently shipped over cleartext
rem LAN. Operator can override by setting WEB_COOKIE_SECURE explicitly.
if not defined WEB_COOKIE_SECURE (
    if /i "%BIND%"=="127.0.0.1" set "WEB_COOKIE_SECURE=0"
    if /i "%BIND%"=="localhost" set "WEB_COOKIE_SECURE=0"
    if /i "%BIND%"=="::1" set "WEB_COOKIE_SECURE=0"
)
if not defined WEB_COOKIE_SECURE (
    echo Refusing to start: BIND '%BIND%' is non-loopback but WEB_COOKIE_SECURE is unset.
    echo Set up TLS and run with WEB_COOKIE_SECURE=1, or set WEB_COOKIE_SECURE=0 to opt in.
    exit /b 1
)

rem Open the browser ~2s later (lets uvicorn bind first), without blocking.
rem Uses ping as a quiet sleep so it won't fight python for the console.
if not defined WEB_NO_BROWSER (
    start "" /b cmd /c "ping -n 3 127.0.0.1 >nul & start %URL%"
)

echo Agent Web UI -^> %URL%   (Ctrl+C to stop)
python -m web --host %BIND% --port %PORT%

endlocal
