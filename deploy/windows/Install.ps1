# Sentinel-FX -- one-command installer for Windows Server / Windows 10+.
#
#   Right-click Install.cmd -> "Run as administrator"
#   or, in an elevated PowerShell:
#       powershell -ExecutionPolicy Bypass -File deploy\windows\Install.ps1
#
# Options:
#   -RunAs User     (default) the engine runs in YOUR logon session. Required
#                   for MetaTrader 5, whose terminal is a desktop program the
#                   Python package can only reach from the same session. Set
#                   up automatic logon for a dedicated Windows user on a VPS.
#   -RunAs Service  the engine starts at boot as SYSTEM, with no logon. For the
#                   paper simulator, OANDA or an MT5 bridge on another machine.
#   -InstallDir / -StateDir / -Port   locations and the dashboard port.
#
# What it does, stopping at the first failure:
#   1. checks the machine (Windows version, RAM, disk, clock, admin rights)
#   2. finds or installs Python 3.11+ (and Node.js if the dashboard must be built)
#      -- downloads are verified by their Authenticode signature
#   3. copies the application, builds a virtualenv, installs dependencies
#      (including MetaTrader5, so MT5 works natively without the bridge)
#   4. generates secrets, writes the configuration, locks the state directory
#      down to SYSTEM, Administrators and the run-as account
#   5. registers scheduled tasks: engine, dead-man watchdog, daily backup
#   6. starts it, waits for the health check, and prints how to log in
#
# IDEMPOTENT: re-running upgrades in place and never overwrites an existing
# secret, configuration or state directory.
[CmdletBinding()]
param(
    [ValidateSet("User", "Service")][string]$RunAs = "User",
    [string]$InstallDir = "C:\SentinelFX\app",
    [string]$StateDir   = "C:\ProgramData\SentinelFX",
    [int]$Port = 8088,
    [switch]$SkipStart
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "common.ps1")
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$SrcDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RunUser = "$env:USERDOMAIN\$env:USERNAME"

# --------------------------------------------------------------------------- #
Step "1/6  Checking this machine"
if (-not (Test-Admin)) { Fail "run this as Administrator (right-click Install.cmd -> Run as administrator)." }

$os = Get-CimInstance Win32_OperatingSystem
Say "OS            $($os.Caption) (build $($os.BuildNumber))"
if ([int]$os.BuildNumber -lt 14393) { Fail "Windows 10 / Server 2016 (build 14393) or newer is required." }
if (-not [Environment]::Is64BitOperatingSystem) { Fail "a 64-bit Windows is required." }

$memMb = [int]($os.TotalVisibleMemorySize / 1024)
Say "memory        $memMb MB"
if ($memMb -lt 1800) { Fail "at least 2 GB of RAM is needed (MetaTrader 5 itself uses ~500 MB)." }

$drive = (Split-Path -Qualifier $InstallDir)
$freeMb = [int]((Get-PSDrive ($drive.TrimEnd(":"))).Free / 1MB)
Say "free disk     $freeMb MB on $drive"
if ($freeMb -lt 3000) { Fail "at least 3 GB free is needed on $drive." }

$w32 = & w32tm /query /status 2>$null
if ($LASTEXITCODE -ne 0 -or -not ($w32 -match "Source")) {
    Warn "the Windows time service is not synchronising. Every decision timestamp depends on the clock."
    Warn "Fix with:  w32tm /config /syncfromflags:manual /manualpeerlist:time.windows.com /update ; w32tm /resync"
} else {
    Say "clock         $((($w32 | Select-String 'Source') -replace '^\s*Source:\s*','').ToString().Trim())"
}

$upgrade = Test-Path -LiteralPath (Join-Path $InstallDir "sentinel")
if ($upgrade) { Say "mode          UPGRADE (existing install at $InstallDir)" } else { Say "mode          FRESH INSTALL" }
Say "run as        $RunAs$(if ($RunAs -eq 'User') { " ($RunUser)" })"

# --------------------------------------------------------------------------- #
Step "2/6  Python and Node.js"
$py = Find-Python
if (-not $py) {
    Say "Python 3.11+ not found; installing Python 3.12 for all users..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install --id Python.Python.3.12 -e --scope machine --silent `
            --accept-package-agreements --accept-source-agreements | Out-Null
    } else {
        $url = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
        $exe = Join-Path $env:TEMP "python-3.12.7-amd64.exe"
        Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
        if (-not (Test-Signed $exe "*Python Software Foundation*")) {
            Remove-Item -LiteralPath $exe -Force
            Fail "the downloaded Python installer is not signed by the Python Software Foundation; refusing to run it."
        }
        $p = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList `
            "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_launcher=1", "Include_test=0"
        if ($p.ExitCode -ne 0) { Fail "the Python installer exited with $($p.ExitCode)." }
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $py = Find-Python
    if (-not $py) { Fail "Python 3.11+ is still not available. Install it from https://www.python.org and re-run." }
}
Say "python        $($py.Version) ($($py.Exe))"

$needNode = -not (Test-Path -LiteralPath (Join-Path $SrcDir "dashboard\dist\index.html"))
$node = Get-Command node -ErrorAction SilentlyContinue
if ($needNode -and -not $node) {
    Say "Node.js is needed to build the dashboard; installing Node.js 20 LTS..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install --id OpenJS.NodeJS.LTS -e --scope machine --silent `
            --accept-package-agreements --accept-source-agreements | Out-Null
    } else {
        $url = "https://nodejs.org/dist/v20.18.0/node-v20.18.0-x64.msi"
        $msi = Join-Path $env:TEMP "node-v20.18.0-x64.msi"
        Invoke-WebRequest -Uri $url -OutFile $msi -UseBasicParsing
        if (-not (Test-Signed $msi "*OpenJS Foundation*")) {
            Remove-Item -LiteralPath $msi -Force
            Fail "the downloaded Node.js installer is not signed by the OpenJS Foundation; refusing to run it."
        }
        $p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList "/i", "`"$msi`"", "/qn"
        if ($p.ExitCode -ne 0) { Warn "the Node.js installer exited with $($p.ExitCode)." }
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $node = Get-Command node -ErrorAction SilentlyContinue
}
if ($node) { Say "node          $(& node --version)" }
elseif ($needNode) { Warn "Node.js is not available: the engine will run, but without the dashboard." }

# --------------------------------------------------------------------------- #
Step "3/6  Installing the application"
New-Item -ItemType Directory -Force -Path $InstallDir, $StateDir | Out-Null
if ($SrcDir -ne $InstallDir) {
    # /MIR keeps the destination identical to the source, EXCEPT for the
    # excluded directories, which are neither copied nor purged: the
    # virtualenv, the dashboard's node_modules and any runtime state.
    & robocopy.exe $SrcDir $InstallDir /MIR /NFL /NDL /NJH /NJS /NP `
        /XD "$SrcDir\var" "$SrcDir\.git" "$SrcDir\.venv" "$SrcDir\dashboard\node_modules" `
            "$InstallDir\.venv" "$InstallDir\dashboard\node_modules" "$InstallDir\var" __pycache__ `
        /XF *.pyc | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "copying the application failed (robocopy exit $LASTEXITCODE)." }
    Say "code copied to $InstallDir"
}

$venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPy) {
    $v = Get-PythonVersion $venvPy
    if (-not $v -or $v -lt $script:MinPython) {
        Warn "the existing virtualenv uses Python $v; rebuilding it"
        Remove-Item -LiteralPath (Join-Path $InstallDir ".venv") -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $venvPy)) {
    & $py.Exe -m venv (Join-Path $InstallDir ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "could not create the virtualenv." }
    Say "virtualenv created"
}
& $venvPy -m pip install --quiet --disable-pip-version-check --upgrade pip
& $venvPy -m pip install --quiet --disable-pip-version-check -r (Join-Path $InstallDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "installing the Python dependencies failed." }
& $venvPy -m pip install --quiet --disable-pip-version-check "MetaTrader5>=5.0.45"
if ($LASTEXITCODE -ne 0) { Warn "the MetaTrader5 package did not install; MT5 brokers will need the bridge." }
Say "python dependencies installed"

$dist = Join-Path $InstallDir "dashboard\dist\index.html"
if ((Get-Command npm -ErrorAction SilentlyContinue) -and
    (-not (Test-Path -LiteralPath $dist) -or $env:REBUILD_DASHBOARD -eq "1")) {
    Say "building the dashboard (this takes a minute)..."
    Push-Location (Join-Path $InstallDir "dashboard")
    try {
        & npm ci --no-audit --no-fund --silent
        if ($LASTEXITCODE -eq 0) { & npm run build --silent }
    } finally { Pop-Location }
    if (Test-Path -LiteralPath $dist) { Say "dashboard built" } else { Warn "the dashboard build failed; the engine will run without it." }
} elseif (Test-Path -LiteralPath $dist) {
    Say "dashboard     present"
}
Protect-CodeDirectory $InstallDir
Say "code          $InstallDir (writable by Administrators and SYSTEM only)"

# --------------------------------------------------------------------------- #
Step "4/6  Configuration and secrets"
$envFile = Join-Path $StateDir "sentinel.env"
$generatedPassword = ""
if (-not (Test-Path -LiteralPath $envFile)) {
    $jwt = New-Secret 48
    $generatedPassword = New-Secret 18
    $lines = @(
        "# Generated by Install.ps1 on $((Get-Date).ToUniversalTime().ToString('s'))Z",
        "# This file contains secrets. Only SYSTEM, Administrators and the run-as account can read it.",
        "SENTINEL_JWT_SECRET=$jwt",
        "# Read ONCE to create the first owner. DELETE these two lines after your first login.",
        "SENTINEL_ADMIN_USER=owner",
        "SENTINEL_ADMIN_PASSWORD=$generatedPassword",
        "SENTINEL_LICENSE=$StateDir\var\licence.key",
        "# MetaTrader 5 on ANOTHER machine through the bridge (leave empty when MT5 runs here):",
        "SENTINEL_MT5_BRIDGE=",
        "SENTINEL_MT5_BRIDGE_TOKEN="
    )
    [IO.File]::WriteAllLines($envFile, $lines, (New-Object Text.UTF8Encoding($false)))
    Say "secrets generated -> $envFile"
} else {
    Say "env file      $envFile (exists, left untouched)"
}

$config = Join-Path $StateDir "config.json"
if (-not (Test-Path -LiteralPath $config)) {
    $code = @"
import importlib.util, sys
from pathlib import Path
sys.path.insert(0, r'$InstallDir')
spec = importlib.util.spec_from_file_location('srv', r'$InstallDir\scripts\serve.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
cfg = mod._default_config_for(Path(r'$config'))
cfg.security.bind_port = $Port
cfg.save(r'$config')
print('default configuration written')
"@
    & $venvPy -c $code
    if ($LASTEXITCODE -ne 0) { Fail "could not write the default configuration." }
} else {
    Say "config        $config (exists, left untouched)"
}
New-Item -ItemType Directory -Force -Path (Join-Path $StateDir "var"), (Join-Path $StateDir "logs"), (Join-Path $StateDir "backups") | Out-Null
if ($RunAs -eq "User") { Protect-Directory $StateDir $RunUser } else { Protect-Directory $StateDir }
Say "state         $StateDir (restricted to SYSTEM, Administrators$(if ($RunAs -eq 'User') { ", $RunUser" }))"

# --------------------------------------------------------------------------- #
Step "5/6  Scheduled tasks"
$runner = Join-Path $InstallDir "deploy\windows\run-engine.ps1"
function New-RunnerAction([string]$role) {
    $arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" " +
           "-InstallDir `"$InstallDir`" -StateDir `"$StateDir`" -Role $role"
    return New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $InstallDir
}
if ($RunAs -eq "Service") {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $RunUser
    $principal = New-ScheduledTaskPrincipal -UserId $RunUser -LogonType Interactive -RunLevel Limited
}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
foreach ($t in @(@($script:EngineTask, "engine"), @($script:WatchdogTask, "watchdog"))) {
    Register-ScheduledTask -TaskName $t[0] -TaskPath $script:TaskPath -Action (New-RunnerAction $t[1]) `
        -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Say "task          $($script:TaskPath)$($t[0])"
}
$backupArg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"" +
             (Join-Path $InstallDir "deploy\windows\Backup.ps1") + "`" -InstallDir `"$InstallDir`" -StateDir `"$StateDir`""
Register-ScheduledTask -TaskName $script:BackupTask -TaskPath $script:TaskPath `
    -Action (New-ScheduledTaskAction -Execute "powershell.exe" -Argument $backupArg) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "03:30") `
    -Principal (New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest) `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable) -Force | Out-Null
Say "task          $($script:TaskPath)$($script:BackupTask) (daily 03:30)"

# --------------------------------------------------------------------------- #
Step "6/6  Starting"
if (-not $SkipStart) {
    foreach ($t in @($script:EngineTask, $script:WatchdogTask)) {
        Start-ScheduledTask -TaskPath $script:TaskPath -TaskName $t
    }
    $healthy = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        if (Get-HealthStatus $Port) { $healthy = $true; break }
    }
    if (-not $healthy) {
        Warn "the engine did not answer on http://127.0.0.1:$Port/health within a minute."
        Warn "Run deploy\windows\Diagnose.ps1, or read $StateDir\logs\engine.log"
        exit 1
    }
    Say "engine        running (http://127.0.0.1:$Port)"
}

$enrol = Join-Path $StateDir "var\enrolment-owner.txt"
Write-Host ""
Write-Host "  ============================================================"
Write-Host "   Sentinel-FX is installed."
Write-Host "  ============================================================"
Write-Host ""
Write-Host "  It starts in ADVISORY mode on the PAPER simulator. It will not"
Write-Host "  place a real order until you deliberately change that."
Write-Host ""
Write-Host "  1. Open http://127.0.0.1:$Port in a browser ON THIS SERVER (RDP)."
Write-Host "     The port is bound to loopback on purpose; do not open it in the firewall."
Write-Host "     From your own PC use an SSH tunnel:  ssh -N -L ${Port}:127.0.0.1:${Port} user@server"
if ($generatedPassword) {
    Write-Host "  2. Log in as:  owner"
    Write-Host "     Password:   $generatedPassword"
    Write-Host "     (shown ONCE; also in $envFile -- delete SENTINEL_ADMIN_PASSWORD there after logging in)"
} else {
    Write-Host "  2. Log in with your existing owner account."
}
Write-Host "  3. Enrol your authenticator app from:  $enrol   -- then DELETE that file."
Write-Host "  4. MetaTrader 5: install the broker's terminal on this machine, sign in, and"
Write-Host "     add the connection on the dashboard's 'Broker' page."
Write-Host ""
Write-Host "  Emergency stop (no password, no network needed):  deploy\windows\Kill-Switch.cmd"
Write-Host "  Health / troubleshooting:                         deploy\windows\Diagnose.cmd"
Write-Host ""
