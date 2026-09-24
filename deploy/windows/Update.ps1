# Sentinel-FX -- upgrade in place, with a backup first and rollback on failure.
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\Update.ps1 -Source C:\Downloads\sentinel-fx-1.5.0
#   powershell -ExecutionPolicy Bypass -File deploy\windows\Update.ps1 -Source C:\Downloads\sentinel-fx-1.5.0.zip
#
# 1. backs up the state (verified) and snapshots the current code
# 2. stops the engine and the watchdog
# 3. runs Install.ps1 from the NEW version (which never overwrites state)
# 4. if the new version does not pass its health check, restores the old code
#    and starts it again
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [string]$InstallDir = "C:\SentinelFX\app",
    [string]$StateDir   = "C:\ProgramData\SentinelFX",
    [ValidateSet("User", "Service")][string]$RunAs = "User",
    [int]$Port = 8088
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
if (-not (Test-Admin)) { Fail "run this as Administrator." }

$work = Join-Path $env:TEMP ("sentinel-update-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
if (Test-Path -LiteralPath $Source -PathType Leaf) {
    if ($Source -like "*.zip") {
        Expand-Archive -LiteralPath $Source -DestinationPath $work
    } else {
        & tar.exe -xzf $Source -C $work
        if ($LASTEXITCODE -ne 0) { Fail "could not unpack $Source" }
    }
    $newRoot = (Get-ChildItem -LiteralPath $work -Directory | Select-Object -First 1).FullName
} else {
    $newRoot = (Resolve-Path $Source).Path
}
if (-not (Test-Path -LiteralPath (Join-Path $newRoot "deploy\windows\Install.ps1"))) {
    Fail "$Source does not look like a Sentinel-FX release (deploy\windows\Install.ps1 missing)."
}

Step "1/4  Backup"
& (Join-Path $PSScriptRoot "Backup.ps1") -InstallDir $InstallDir -StateDir $StateDir
if ($LASTEXITCODE -ne 0) { Fail "the backup failed; nothing was changed." }
$snapshot = "$InstallDir.previous"
if (Test-Path -LiteralPath $snapshot) { Remove-Item -LiteralPath $snapshot -Recurse -Force }
& robocopy.exe $InstallDir $snapshot /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Fail "could not snapshot the current code; nothing was changed." }

Step "2/4  Stopping"
foreach ($t in @($script:EngineTask, $script:WatchdogTask)) {
    Stop-ScheduledTask -TaskPath $script:TaskPath -TaskName $t -ErrorAction SilentlyContinue
}
foreach ($p in @(Get-EngineProcess $InstallDir)) { Stop-Process -Id $p.ProcessId -Force }
Start-Sleep -Seconds 3

Step "3/4  Installing the new version"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $newRoot "deploy\windows\Install.ps1") `
    -RunAs $RunAs -InstallDir $InstallDir -StateDir $StateDir -Port $Port
$ok = ($LASTEXITCODE -eq 0) -and (Get-HealthStatus $Port)

Step "4/4  Verifying"
if ($ok) {
    Say "updated; the previous code is kept at $snapshot until the next update."
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    exit 0
}
Warn "the new version did not come up healthy; rolling back the code."
foreach ($t in @($script:EngineTask, $script:WatchdogTask)) {
    Stop-ScheduledTask -TaskPath $script:TaskPath -TaskName $t -ErrorAction SilentlyContinue
}
foreach ($p in @(Get-EngineProcess $InstallDir)) { Stop-Process -Id $p.ProcessId -Force }
& robocopy.exe $snapshot $InstallDir /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
foreach ($t in @($script:EngineTask, $script:WatchdogTask)) {
    Start-ScheduledTask -TaskPath $script:TaskPath -TaskName $t
}
Fail "rolled back to the previous version. Read $StateDir\logs\engine.log for why the new one failed."
