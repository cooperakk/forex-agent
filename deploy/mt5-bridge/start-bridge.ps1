# Sentinel-FX -- run the MetaTrader 5 bridge on this Windows machine.
#
#   Right-click -> "Run with PowerShell", or from a PowerShell window:
#       .\start-bridge.ps1
#
# What it does:
#   1. finds Python, creates a private virtualenv next to this script
#   2. installs the MetaTrader5 package into it (once)
#   3. starts scripts\mt5_bridge.py, which attaches to the RUNNING, SIGNED-IN
#      MetaTrader 5 terminal and serves it on 127.0.0.1:5555
#
# The first run prints a TOKEN once and saves it in bridge.token. Copy that
# token to the Ubuntu server:  sudo /opt/sentinel-fx/deploy/scripts/connect-mt5.sh
#
# Keep this window open. Closing it stops the bridge (the engine on the
# server then stops taking new trades and says why; open positions keep their
# server-side stops at the broker).
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $here "..\..")
$venv = Join-Path $here ".venv"

function Say($m) { Write-Host "[bridge] $m" }

# --- 1. python -------------------------------------------------------------
$py = $null
foreach ($cand in @("python", "py")) {
    try {
        $v = & $cand --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$v" -match "Python 3\.(1[1-9]|[2-9][0-9])") { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    Write-Host ""
    Write-Host "  Python 3.11 or newer was not found."
    Write-Host "  Install it from https://www.python.org/downloads/windows/"
    Write-Host "  and tick 'Add python.exe to PATH' in the installer. Then run this again."
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}
Say "python: $(& $py --version 2>&1)"

# --- 2. virtualenv + MetaTrader5 -------------------------------------------
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Say "creating a virtualenv (once)..."
    & $py -m venv $venv
}
$vpy = Join-Path $venv "Scripts\python.exe"
& $vpy -c "import MetaTrader5" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say "installing the MetaTrader5 package (once)..."
    & $vpy -m pip install --quiet --upgrade pip
    & $vpy -m pip install --quiet MetaTrader5
}

# --- 3. is the terminal running? -------------------------------------------
$term = Get-Process -Name "terminal64" -ErrorAction SilentlyContinue
if (-not $term) {
    Write-Host ""
    Write-Host "  MetaTrader 5 is not running. Open it, sign in to your DEMO account,"
    Write-Host "  wait for 'Connected' at the bottom-right, then run this again."
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

# --- 4. serve --------------------------------------------------------------
Say "starting the bridge on 127.0.0.1:5555 (Ctrl+C stops it)"
Set-Location $root
& $vpy (Join-Path $root "scripts\mt5_bridge.py") --token-file (Join-Path $here "bridge.token")
