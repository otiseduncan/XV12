@echo off
setlocal
title XODUZ XV12 Launcher
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-xv12.ps1"
set "XODUZ_EXIT=%ERRORLEVEL%"
if not "%XODUZ_EXIT%"=="0" (
  echo.
  echo XODUZ XV12 did not start. Review logs\launcher.log for details.
  pause
)
exit /b %XODUZ_EXIT%
