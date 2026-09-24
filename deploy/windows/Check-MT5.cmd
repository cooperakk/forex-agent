@echo off
REM Sentinel-FX -- runs Check-MT5.ps1 with the execution policy bypassed for this script only.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Check-MT5.ps1" %*
echo.
pause
