@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "KILLED=0"
rem 按项目路径精确停止 web.py，避免 PID 文件记录到 System 或其他监听程序。
for /f %%p in ('powershell -NoProfile -Command "$root=[regex]::Escape((Get-Location).Path); $all=Get-CimInstance Win32_Process; foreach($x in $all){ if(($x.Name -eq 'python.exe' -or $x.Name -eq 'pythonw.exe') -and $x.CommandLine -match ($root+'\\.*web\\.py') -and $x.CommandLine -match '--port (5000|5001|5002)'){ $x.ProcessId } }"') do (
  taskkill /PID %%p /T /F >nul 2>&1 && set "KILLED=1"
)
if exist run\webui.pid del /f /q run\webui.pid >nul 2>&1
rem 端口对应的 Python 进程再兜底处理；System PID 自动跳过。
for %%a in (5000 5001 5002) do (
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":%%a .*LISTENING"') do (
    tasklist /FI "PID eq %%p" 2>nul | findstr /I "python.exe pythonw.exe" >nul && (taskkill /PID %%p /T /F >nul 2>&1 && set "KILLED=1")
  )
)
set "REMAINING="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":5000 .*LISTENING" /C:":5001 .*LISTENING" /C:":5002 .*LISTENING"') do set "REMAINING=%%p"
if defined REMAINING (
  echo WebUI python process stopped, but port !REMAINING! is still held by a system listener or another service.
) else if "%KILLED%"=="1" (
  echo WebUI stopped and ports 5000-5002 are closed.
) else (
  echo WebUI was not running; ports 5000-5002 are closed.
)
exit /b 0
