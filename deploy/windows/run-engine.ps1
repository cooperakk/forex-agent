# Sentinel-FX -- engine supervisor for Windows (started by the scheduled task).
#
# Loads the environment file, runs scripts\serve.py, and restarts it if it
# dies -- but with the same brake systemd has on Linux: five crashes inside ten
# minutes and it stops and waits for a human. An engine that crashes on every
# start for a reason nobody can see (a bad credential, a corrupt registry) must
# not be restarted forever, because every restart is another chance to act on
# an order whose fate is unknown.
#
# Stopping: the task is ended (Stop-ScheduledTask) or the process is killed.
# The engine is built to survive an abrupt stop -- its state file is fsync'd,
# order ids are deterministic, and the first thing it does on start is replay
# its journal and reconcile against the broker.
param(
    [string]$InstallDir = "C:\SentinelFX\app",
    [string]$StateDir   = "C:\ProgramData\SentinelFX",
    [ValidateSet("engine", "watchdog")][string]$Role = "engine"
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$logDir = Join-Path $StateDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "$Role.log"
$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
$config = Join-Path $StateDir "config.json"
$var = Join-Path $StateDir "var"

function Log([string]$m) {
    $line = "{0} [supervisor] {1}" -f (Get-Date).ToUniversalTime().ToString("s"), $m
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

function Rotate {
    if ((Test-Path -LiteralPath $log) -and ((Get-Item -LiteralPath $log).Length -gt 20MB)) {
        $old = "$log.1"
        if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Force }
        Move-Item -LiteralPath $log -Destination $old -Force
    }
}

# Environment: the secrets file, then the fixed settings (last one wins, as in
# the systemd unit).
$envFile = Join-Path $StateDir "sentinel.env"
$vars = Read-EnvFile $envFile
foreach ($k in $vars.Keys) { [Environment]::SetEnvironmentVariable($k, $vars[$k], "Process") }
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:SENTINEL_CONFIG = $config
$env:TZ = "UTC"

if ($Role -eq "engine") {
    $argv = @("scripts\serve.py", "--config", $config,
              "--dashboard", (Join-Path $InstallDir "dashboard\dist"))
} else {
    $argv = @("-m", "sentinel.ops.watchdog",
              "--heartbeat", (Join-Path $var "heartbeat.json"),
              "--kill-file", (Join-Path $var "KILL"),
              "--audit", (Join-Path $var "watchdog.jsonl"),
              "--poll", "5")
}

$crashes = New-Object System.Collections.Generic.List[datetime]
while ($true) {
    Rotate
    Log "starting $Role"
    $started = Get-Date
    Push-Location $InstallDir
    try {
        & $python @argv *>> $log
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    Log "$Role exited with code $code after $([int]((Get-Date) - $started).TotalSeconds)s"
    $now = Get-Date
    $crashes.Add($now)
    $recent = @($crashes | Where-Object { ($now - $_).TotalMinutes -lt 10 })
    if ($recent.Count -ge 5) {
        Log "five exits inside ten minutes: NOT restarting. A human must look at $log and start the task again."
        exit 1
    }
    Start-Sleep -Seconds 10
}
