@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
rem Prefer the system Node.js installation so npm is available in the runtime self-check.
if exist "%ProgramFiles%\nodejs" set "PATH=%ProgramFiles%\nodejs;%PATH%"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PORT=%~1
if "%PORT%"=="" set PORT=5001

if not exist logs mkdir logs
if not exist run mkdir run

echo [CLEANUP] Stopping old WebUI listeners on ports 5000, 5001 and 5002...
for %%a in (5000 5001 5002) do (
  call :stop_port %%a
  if errorlevel 1 goto failed
)
if exist run\webui.pid del /f /q run\webui.pid >nul 2>&1

if not exist .venv\Scripts\python.exe (
  echo [ERR] .venv not found. Run install-integrations.bat first.
  goto failed
)
".venv\Scripts\python.exe" tools\check_integrations.py
if errorlevel 1 (
  echo [ERR] Runtime self-check failed. Run install-integrations.bat to repair it.
  goto failed
)
echo Starting WebUI on http://127.0.0.1:%PORT% ...
start /B "" ".venv\Scripts\python.exe" web.py --host 127.0.0.1 --port %PORT% > logs\webui-%PORT%.log 2>&1
for /l %%i in (1,1,30) do (
  ping 127.0.0.1 -n 2 >nul
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    echo %%p> run\webui.pid
    echo Started PID=%%p
    echo Auth code is in .env WEBUI_AUTH_CODE
    start "" "http://127.0.0.1:%PORT%/"
    exit /b 0
  )
)
echo Start failed, see logs\webui-%PORT%.log
goto failed

:stop_port
set "OLD_PORT=%~1"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":%OLD_PORT% .*LISTENING"') do (
  echo [CLEANUP] Port %OLD_PORT% PID=%%p
  taskkill /PID %%p /T /F >nul 2>&1
)
for /l %%i in (1,1,10) do (
  netstat -ano | findstr /R /C:":%OLD_PORT% .*LISTENING" >nul || exit /b 0
  ping 127.0.0.1 -n 2 >nul
)
echo [ERR] Could not release port %OLD_PORT%.
exit /b 1

:failed
echo.
echo Press any key to close this window.
pause >nul
exit /b 1
