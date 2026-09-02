@echo off
setlocal
cd /d "%~dp0.."
".venv\Scripts\python.exe" "momo_qualification_checker\app.py" --host 127.0.0.1 --port 5013
