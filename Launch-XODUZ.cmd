@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-xv12.ps1"
if errorlevel 1 (
  echo.
  echo XODUZ XV12 did not start. Review logs\launcher.log for details.
  pause
)
