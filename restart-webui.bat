@echo off
setlocal EnableExtensions
rem Starting already replaces the old instance; keep a single restart path.
call "%~dp0start-webui.bat" %*
exit /b %errorlevel%
