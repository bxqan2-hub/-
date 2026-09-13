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
set "EXISTING_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do if not defined EXISTING_PID set "EXISTING_PID=%%p"
if defined EXISTING_PID (
  ".venv\Scripts\python.exe" -c "import http.client,os,re; from pathlib import Path; c=http.client.HTTPConnection('127.0.0.1',int(os.environ['PORT']),timeout=3); c.request('GET','/login'); r=c.getresponse(); body=r.read(65536).decode('utf-8'); title=re.search(r'<title>.*?</title>',Path('webui/templates/login.html').read_text(encoding='utf-8')).group(0); assert r.status==200 and title in body and ('name='+chr(34)+'auth_code'+chr(34)) in body and ('method='+chr(34)+'post'+chr(34)) in body; c.close()" >nul 2>&1
  if not errorlevel 1 (
    echo WebUI is already running on http://127.0.0.1:%PORT% ^(PID %EXISTING_PID%^).
    start "" "http://127.0.0.1:%PORT%/"
    exit /b 0
  )
  echo Existing listener did not answer as this WebUI; attempting to start the project instance.
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
  for /f %%p in ('powershell -NoProfile -Command "$all=Get-CimInstance Win32_Process; foreach($x in $all){if($x.Name -eq 'python.exe' -and $x.CommandLine -match 'web.py' -and $x.CommandLine -match '--port %PORT%'){ $x.ProcessId; break }}"') do (
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
