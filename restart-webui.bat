@echo off
cd /d %~dp0
call stop-webui.bat
ping 127.0.0.1 -n 2 >nul
call start-webui.bat %1
