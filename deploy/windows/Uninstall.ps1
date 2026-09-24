# Sentinel-FX -- remove the application from this Windows machine.
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\Uninstall.ps1 [-PurgeState]
#
# By default the STATE is kept: the audit chain, the account store, the
# verdict registry and the drawdown ladder's memory. Deleting them is a
# separate, explicit decision (-PurgeState, and typing DELETE), because a
# reinstall that starts from nothing resumes at full size after a drawdown.
# Positions at the broker are NOT touched either way -- close them first if
# that is what you want.
param(
    [string]$InstallDir = "C:\SentinelFX\app",
    [string]$StateDir   = "C:\ProgramData\SentinelFX",
    [switch]$PurgeState
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
if (-not (Test-Admin)) { Fail "run this as Administrator." }

foreach ($t in @($script:EngineTask, $script:WatchdogTask, $script:BackupTask)) {
    $task = Get-ScheduledTask -TaskPath $script:TaskPath -TaskName $t -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskPath $script:TaskPath -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskPath $script:TaskPath -TaskName $t -Confirm:$false
        Say "removed task $t"
    }
}
foreach ($p in @(Get-EngineProcess $InstallDir)) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    Say "stopped process $($p.ProcessId)"
}
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*sentinel.ops.watchdog*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if (Test-Path -LiteralPath $InstallDir) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
    Say "removed $InstallDir"
}
if ($PurgeState) {
    Warn "This deletes ${StateDir}: the audit chain, accounts, verdicts and risk state."
    $answer = Read-Host "Type DELETE to confirm"
    if ($answer -eq "DELETE") {
        Remove-Item -LiteralPath $StateDir -Recurse -Force
        Say "removed $StateDir"
    } else {
        Say "state kept at $StateDir"
    }
} else {
    Say "state kept at $StateDir (use -PurgeState to delete it)"
}
