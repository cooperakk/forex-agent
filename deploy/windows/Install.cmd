@echo off
REM Sentinel-FX -- runs Install.ps1 with the execution policy bypassed for this script only.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1" %*
echo.
pause
