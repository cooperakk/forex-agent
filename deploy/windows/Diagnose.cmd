@echo off
REM Sentinel-FX -- runs Diagnose.ps1 with the execution policy bypassed for this script only.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Diagnose.ps1" %*
echo.
pause
