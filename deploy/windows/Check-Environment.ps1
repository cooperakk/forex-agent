# Sentinel-FX -- can this Windows machine run the engine?  (run BEFORE install)
#
#   Double-click Check-Environment.cmd, or:
#       powershell -ExecutionPolicy Bypass -File deploy\windows\Check-Environment.ps1 [-Quick]
#
# Read-only: installs nothing, changes nothing. Exit code 0 = ready,
# 1 = works with warnings, 2 = will not work as is.
param([switch]$Quick, [int]$Port = 8088)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "common.ps1")
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$script:fails = 0; $script:warns = 0
function Sec([string]$m) { Write-Host ""; Write-Host "== $m ==" -ForegroundColor DarkGray }
function Ok([string]$m)   { Write-Host "   ok   $m" -ForegroundColor Green }
function W([string]$m)    { Write-Host "   warn $m" -ForegroundColor Yellow; $script:warns++ }
function Bad([string]$m)  { Write-Host "   FAIL $m" -ForegroundColor Red; $script:fails++ }
function Hint([string]$m) { Write-Host "        -> $m" -ForegroundColor DarkGray }

Sec "System"
$os = Get-CimInstance Win32_OperatingSystem
if ([int]$os.BuildNumber -ge 14393) { Ok "$($os.Caption) (build $($os.BuildNumber))" }
else { Bad "$($os.Caption) is too old; Windows 10 / Server 2016 or newer is required" }
if ([Environment]::Is64BitOperatingSystem) { Ok "64-bit" } else { Bad "32-bit Windows is not supported" }
if (Test-Admin) { Ok "running as Administrator" } else { W "not elevated -- the installer must run as Administrator" }
Ok "PowerShell $($PSVersionTable.PSVersion)"

$memMb = [int]($os.TotalVisibleMemorySize / 1024)
if ($memMb -ge 4000) { Ok "memory: $memMb MB" }
elseif ($memMb -ge 1800) { W "memory: $memMb MB -- works; MetaTrader 5 plus research runs prefer 4 GB" }
else { Bad "memory: $memMb MB -- at least 2 GB is required" }

$c = Get-PSDrive C -ErrorAction SilentlyContinue
if ($c) {
    $freeMb = [int]($c.Free / 1MB)
    if ($freeMb -ge 3000) { Ok "free disk on C: $freeMb MB" } else { Bad "free disk on C: $freeMb MB -- 3 GB is required" }
}

Sec "Clock"
$w32 = & w32tm /query /status 2>$null
if ($LASTEXITCODE -eq 0 -and ($w32 -match "Source")) {
    $src = (($w32 | Select-String "Source") -replace "^\s*Source:\s*", "").ToString().Trim()
    $last = (($w32 | Select-String "Last Successful Sync Time") -replace "^\s*Last Successful Sync Time:\s*", "")
    if ($src -like "*Local CMOS Clock*" -or $src -like "*Free-running*") {
        Bad "the clock is not synchronised with any time server (source: $src)"
        Hint "w32tm /config /syncfromflags:manual /manualpeerlist:time.windows.com /update ; w32tm /resync"
    } else {
        Ok "time source: $src (last sync: $last)"
    }
} else {
    Bad "the Windows Time service is not running"
    Hint "Start-Service w32time ; w32tm /resync"
}

Sec "Software"
$py = Find-Python
if ($py) { Ok "python $($py.Version) ($($py.Exe))" }
else { W "Python 3.11+ not found -- the installer will install Python 3.12" }
$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) { Ok "node $(& node --version)" } else { W "Node.js not found -- needed only to build the dashboard" }
if (Get-Command winget -ErrorAction SilentlyContinue) { Ok "winget available" }
else { W "winget not available -- the installer will download signed installers directly" }
$policy = Get-ExecutionPolicy -Scope LocalMachine
Ok "execution policy (machine): $policy (the .cmd wrappers bypass it for these scripts only)"
$longPaths = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
if ($longPaths -eq 1) { Ok "long paths enabled" } else { W "long paths are disabled; deep node_modules paths may fail to build" ; Hint "Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem LongPathsEnabled 1" }

Sec "MetaTrader 5"
$terminals = @()
foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($root -and (Test-Path -LiteralPath $root)) {
        $terminals += @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "terminal64.exe" } |
            Where-Object { Test-Path -LiteralPath $_ })
    }
}
if ($terminals.Count -gt 0) { foreach ($t in $terminals) { Ok "terminal found: $t" } }
else { W "no MetaTrader 5 terminal found -- install your broker's MT5 if you trade through MetaTrader" }
if (Get-Process -Name terminal64 -ErrorAction SilentlyContinue) { Ok "a terminal is running" }

Sec "Network"
$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($inUse) { Bad "port $Port is already in use (process $($inUse[0].OwningProcess))" } else { Ok "port $Port is free" }
$rule = Get-NetFirewallPortFilter -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq "$Port" }
if ($rule) { W "a firewall rule mentions port $Port -- the dashboard must NOT be exposed; use RDP or an SSH tunnel" }

if (-not $Quick) {
    Sec "Outbound HTTPS"
    $targets = @(
        @("PyPI (packages)", "https://pypi.org/simple/pip/", $true),
        @("Python wheels", "https://files.pythonhosted.org/", $true),
        @("npm registry", "https://registry.npmjs.org/", $false),
        @("economic calendar", "https://nfs.faireconomy.media/ff_calendar_thisweek.json", $false),
        @("Federal Reserve feed", "https://www.federalreserve.gov/feeds/press_all.xml", $false),
        @("Claude API", "https://api.anthropic.com/", $false),
        @("OpenAI API", "https://api.openai.com/", $false),
        @("Gemini API", "https://generativelanguage.googleapis.com/", $false),
        @("DeepSeek API", "https://api.deepseek.com/", $false),
        @("Kimi (Moonshot) API", "https://api.moonshot.ai/", $false),
        @("Jev (TypeSafe) API", "https://api.typesafe.ai/", $false),
        @("TradingView data (reference)", "https://data.tradingview.com/", $false),
        @("TradingView scanner (ratings)", "https://scanner.tradingview.com/", $false)
    )
    foreach ($t in $targets) {
        $reached = $false
        try {
            Invoke-WebRequest -Uri $t[1] -Method Head -UseBasicParsing -TimeoutSec 8 | Out-Null
            $reached = $true
        } catch {
            # An HTTP error status still proves the host answered.
            if ($_.Exception.Response) { $reached = $true }
        }
        if ($reached) { Ok "$($t[0]) reachable" }
        elseif ($t[2]) { Bad "$($t[0]) NOT reachable ($($t[1]))" }
        else { W "$($t[0]) not reachable -- only matters if you use it" }
    }
    Hint "an unreachable news, AI or TradingView endpoint only disables that feature; trading is unaffected"
    Hint "full TradingView test (websocket + ratings): .venv\Scripts\python.exe scripts\tv_history.py --selftest"
}

Sec "Summary"
if ($script:fails -gt 0) {
    Write-Host "  $($script:fails) problem(s) must be fixed before installing." -ForegroundColor Red
    exit 2
}
if ($script:warns -gt 0) {
    Write-Host "  Ready, with $($script:warns) warning(s). Next: Install.cmd (as Administrator)" -ForegroundColor Yellow
    exit 1
}
Write-Host "  Ready. Next: Install.cmd (as Administrator)" -ForegroundColor Green
exit 0
