@echo off
rem Daily morning monitor - registered in Task Scheduler by register_task.bat
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0.."
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
if not exist logs mkdir logs
%PY% -m uvis_monitor run >> logs\scheduler.log 2>&1
