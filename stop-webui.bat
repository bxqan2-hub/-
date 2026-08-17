@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set KILLED=0
if exist run\webui.pid (
  for /f %%p in (run\webui.pid) do (
    taskkill /PID %%p /T /F >nul 2>&1 && set KILLED=1
  )
  del /f /q run\webui.pid >nul 2>&1
)
for %%a in (5000 5001 5002) do (
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":%%a .*LISTENING"') do (
    taskkill /PID %%p /T /F >nul 2>&1 && set KILLED=1
  )
)
if "%KILLED%"=="1" (echo WebUI stopped) else (echo WebUI was not running)
