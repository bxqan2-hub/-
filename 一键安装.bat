@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d %~dp0

rem Refresh common locations because winget cannot update this cmd.exe PATH.
set "PATH=%ProgramFiles%\nodejs;%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%PATH%"

where node >nul 2>&1
if errorlevel 1 (
  where winget >nul 2>&1
  if errorlevel 1 goto missing_node
  echo [SETUP] Node.js is missing. Installing Node.js LTS with winget...
  winget install --id OpenJS.NodeJS.LTS -e --silent --accept-package-agreements --accept-source-agreements
  if errorlevel 1 goto install_node_failed
  set "PATH=%ProgramFiles%\nodejs;%PATH%"
)

where py >nul 2>&1
if not errorlevel 1 goto prerequisites_ready
where python >nul 2>&1
if not errorlevel 1 goto prerequisites_ready
where winget >nul 2>&1
if errorlevel 1 goto missing_python
echo [SETUP] Python is missing. Installing Python 3.12 with winget...
winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto install_python_failed
set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%PATH%"

:prerequisites_ready
call install-integrations.bat
set RESULT=%ERRORLEVEL%
echo.
if not "%RESULT%"=="0" (
  echo Installation failed. Check the first [ERR] message above.
) else (
  echo Installation succeeded. Double-click start-webui.bat to start everything.
)
pause
exit /b %RESULT%

:missing_node
echo [ERR] Node.js is missing and winget is unavailable.
echo Install Node.js 18 or newer, then run this file again.
goto failed

:missing_python
echo [ERR] Python is missing and winget is unavailable.
echo Install Python 3.10 or newer, then run this file again.
goto failed

:install_node_failed
echo [ERR] winget could not install Node.js LTS.
goto failed

:install_python_failed
echo [ERR] winget could not install Python 3.12.

:failed
echo.
pause
exit /b 1
