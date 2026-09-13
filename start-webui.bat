@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
rem Prefer the installed Node.js runtime for the dependency self-check.
if exist "%ProgramFiles%\nodejs" set "PATH=%ProgramFiles%\nodejs;%PATH%"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PORT=%~1"
if not defined PORT set "PORT=5002"

if not exist .venv\Scripts\python.exe (
  echo [ERR] .venv not found. Run install-integrations.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" -c "import os,sys; v=os.environ['PORT']; sys.exit(0 if v.isascii() and v.isdecimal() and len(v)<=5 and 1<=int(v)<=65535 else 1)"
if errorlevel 1 (
  echo [ERR] Python could not start, or the port is not an integer from 1 to 65535.
  exit /b 1
)
for /f %%p in ('.venv\Scripts\python.exe -c "import os; print(int(os.environ['PORT']))"') do set "PORT=%%p"
rem Validate dependencies before interrupting the previous instance.
".venv\Scripts\python.exe" tools\check_integrations.py
if errorlevel 1 (
  echo [ERR] Runtime self-check failed. Run install-integrations.bat to repair it.
  exit /b 1
)
if not exist logs mkdir logs
if not exist run mkdir run
rem The shared stop path replaces this project and the requested port without prompting.
call "%~dp0stop-webui.bat" "%PORT%"
if errorlevel 1 exit /b 1

echo Starting WebUI on http://127.0.0.1:%PORT% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $root=(Get-Location).Path; $python=Join-Path $root '.venv\Scripts\python.exe'; $argsLine=[char]34+(Join-Path $root 'web.py')+[char]34+' --host 127.0.0.1 --port '+$env:PORT; $child=Start-Process -FilePath $python -ArgumentList $argsLine -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root ('logs\webui-'+$env:PORT+'.log')) -RedirectStandardError (Join-Path $root ('logs\webui-'+$env:PORT+'.err.log')) -PassThru; Set-Content -LiteralPath (Join-Path $root 'run\webui.pid') -Value $child.Id -Encoding ASCII"
if errorlevel 1 exit /b 1
rem A process existing is not readiness. Wait for the actual login page with no proxy.
for /l %%i in (1,1,30) do (
  ".venv\Scripts\python.exe" -c "import http.client,os; c=http.client.HTTPConnection('127.0.0.1',int(os.environ['PORT']),timeout=1); c.request('GET','/login'); r=c.getresponse(); b=r.read(65536).decode('utf-8'); assert r.status==200 and 'name='+chr(34)+'auth_code'+chr(34) in b; c.close()" >nul 2>&1
  if not errorlevel 1 goto ready
  ping 127.0.0.1 -n 2 >nul
)
echo [ERR] Startup did not become ready. See logs\webui-%PORT%.log and logs\webui-%PORT%.err.log
exit /b 1

:ready
rem Store the real listener PID, not the virtual-environment launcher PID.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $root=(Get-Location).Path; $entry=[regex]::Escape((Join-Path $root 'web.py')); $owners=@(Get-NetTCPConnection -LocalPort ([int]$env:PORT) -State Listen | Select-Object -ExpandProperty OwningProcess -Unique); $matched=@($owners | ForEach-Object {Get-CimInstance Win32_Process -Filter ('ProcessId='+$_)} | Where-Object {$_.CommandLine -match $entry}); if($matched.Count -ne 1){throw 'Started listener identity changed'}; Set-Content -LiteralPath (Join-Path $root 'run\webui.pid') -Value $matched[0].ProcessId -Encoding ASCII; Write-Host ('Started PID='+$matched[0].ProcessId)"
if errorlevel 1 exit /b 1
echo Ready: http://127.0.0.1:%PORT%/
start "" "http://127.0.0.1:%PORT%/"
exit /b 0
