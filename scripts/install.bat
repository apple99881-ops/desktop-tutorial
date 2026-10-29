@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ from python.org and check "Add python.exe to PATH".
  pause
  exit /b 1
)
echo Installing packages...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Package installation failed. Check the internet connection and try again.
  pause
  exit /b 1
)
echo Installing Chromium for UVIS web automation...
%PY% -m playwright install chromium
if not exist config\config.yaml copy config\config.example.yaml config\config.yaml >nul
if not exist config\routes.yaml copy config\routes.example.yaml config\routes.yaml >nul
if not exist .env copy .env.example .env >nul
echo.
echo [OK] Installation complete.
echo   1. Run scripts\run_demo.bat to see a demo.
echo   2. Edit config\routes.yaml (your routes) and run:  %PY% -m uvis_monitor check
echo   3. Edit .env and config\config.yaml (messenger, UVIS). See docs folder.
pause
