@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0.."
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -m uvis_monitor demo
echo.
if exist reports\demo start "" "reports\demo"
pause
