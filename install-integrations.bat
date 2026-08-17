@echo off
setlocal
chcp 65001 >nul
cd /d %~dp0

echo ============================================================
echo Installing the complete PAY153 runtime...
echo Project: %CD%
echo ============================================================

where node >nul 2>&1
if errorlevel 1 (
  echo [ERR] Node.js was not found. Install Node.js 18 or newer first.
  exit /b 1
)
where npm >nul 2>&1
if errorlevel 1 (
  echo [ERR] npm was not found. Reinstall Node.js with npm enabled.
  exit /b 1
)
for /f "tokens=1 delims=." %%v in ('node -p "process.versions.node"') do set NODE_MAJOR=%%v
if %NODE_MAJOR% LSS 18 (
  echo [ERR] Node.js 18 or newer is required. Current version:
  node --version
  exit /b 1
)

echo [1/6] Creating a portable Python virtual environment...
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -m venv --clear .venv
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo [ERR] Python was not found. Install Python 3.10 or newer first.
    exit /b 1
  )
  python -m venv --clear .venv
)
if errorlevel 1 exit /b 1

echo [2/6] Installing Python dependencies for the site and both integrations...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r integrations\pay153_checkout\requirements.txt -r integrations\paypal_agreement_protocol\requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install pytest
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pytest --version
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 exit /b 1

echo [3/6] Installing locked PAY153 Sentinel Node.js dependencies...
call npm ci --prefix integrations\pay153_checkout --no-audit --no-fund
if errorlevel 1 exit /b 1

echo [4/6] Installing Playwright Chromium runtime...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 exit /b 1

echo [5/6] Preparing the optional UPI engine...
where go >nul 2>&1
if errorlevel 1 goto check_prebuilt_go
pushd integrations\pay153_checkout\tools\upi_go
go build -o pix_extract_slot.exe .
if errorlevel 1 (
  popd
  exit /b 1
)
popd
goto verify

:check_prebuilt_go
if exist integrations\pay153_checkout\tools\upi_go\pix_extract_slot.exe (
  echo [OK] Go is not installed; using the bundled Windows UPI engine.
) else (
  echo [WARN] Go is not installed and no prebuilt UPI engine exists.
  echo [WARN] Install Go and run this script again if PIX UPI extraction is required.
)

:verify
echo [6/6] Running full integration self-check...
".venv\Scripts\python.exe" tools\check_integrations.py --launch-browser
if errorlevel 1 exit /b 1

echo ============================================================
echo [OK] Installation completed. Run start-webui.bat to start everything.
echo ============================================================
exit /b 0
