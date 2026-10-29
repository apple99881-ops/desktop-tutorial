@echo off
rem Run as Administrator. Registers the morning monitor for weekdays at 05:50.
schtasks /Create /TN "UVIS_Morning_Monitor" /TR "\"%~dp0run_monitor.bat\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 05:50 /F
if errorlevel 1 (
  echo [ERROR] Registration failed. Right-click this file and choose "Run as administrator".
  pause
  exit /b 1
)
echo [OK] Registered task "UVIS_Morning_Monitor" : weekdays 05:50
echo      Open Task Scheduler to enable "Wake the computer to run this task" if the PC sleeps.
pause
