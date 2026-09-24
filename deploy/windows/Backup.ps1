# Sentinel-FX -- back up the state that cannot be re-fetched from the broker.
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\Backup.ps1 [-Dest D:\backups]
#
# Runs daily from the scheduled task. SQLite files are copied through the
# database's own backup API and the audit chain is verified inside the copy
# (scripts\backup_state.py). The credential KEY is deliberately left out --
# keep a copy of var\broker-secrets.key somewhere else.
param(
    [string]$InstallDir = "C:\SentinelFX\app",
    [string]$StateDir   = "C:\ProgramData\SentinelFX",
    [string]$Dest       = "",
    [int]$KeepDays      = 30
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
if (-not $Dest) { $Dest = Join-Path $StateDir "backups" }
$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
& $python (Join-Path $InstallDir "scripts\backup_state.py") --state $StateDir --dest $Dest --keep-days $KeepDays
exit $LASTEXITCODE
