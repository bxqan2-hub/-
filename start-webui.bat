@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
rem Prefer the system Node.js installation so npm is available in the runtime self-check.
if exist "%ProgramFiles%\nodejs" set "PATH=%ProgramFiles%\nodejs;%PATH%"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PORT=%~1"
if not defined PORT set "PORT=5001"

if not exist logs mkdir logs
if not exist run mkdir run

if not exist .venv\Scripts\python.exe (
  echo [ERR] .venv not found. Run install-integrations.bat first.
  goto failed
)
".venv\Scripts\python.exe" -c "import os, sys; value = os.environ['PORT']; sys.exit(0 if value.isascii() and value.isdecimal() and len(value) <= 5 and 1 <= int(value) <= 65535 else 1)"
if errorlevel 1 (
  echo [ERR] Python could not start, or the port is not an integer from 1 to 65535.
  goto failed
)
for /f %%p in ('.venv\Scripts\python.exe -c "import os; print(int(os.environ['PORT']))"') do set "PORT=%%p"
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
  echo [ERR] Port %PORT% is already in use. Existing services were left running.
  echo Use the running WebUI or start-webui.bat with a different port.
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

:failed
echo.
echo Press any key to close this window.
pause >nul
exit /b 1
