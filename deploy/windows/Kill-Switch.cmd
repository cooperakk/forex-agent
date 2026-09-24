@echo off
REM Sentinel-FX -- runs Kill-Switch.ps1 with the execution policy bypassed for this script only.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Kill-Switch.ps1" %*
echo.
pause
