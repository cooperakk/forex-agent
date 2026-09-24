# Sentinel-FX -- why will MetaTrader 5 not connect? Read-only: never trades.
#
#   Double-click Check-MT5.cmd, or:
#       powershell -ExecutionPolicy Bypass -File deploy\windows\Check-MT5.ps1
#       powershell -ExecutionPolicy Bypass -File deploy\windows\Check-MT5.ps1 -Login 12345678 -Server "Alpari-MT5-Demo"
#
# Asks for the trading password without echoing it, hands it to the checker
# through a process-only environment variable, removes it afterwards, and
# opens a Persian report in the browser.
param(
    [string]$Login = "",
    [string]$Server = "",
    [string]$Path = "",
    [string]$InstallDir = "C:\SentinelFX\app"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$candidates = @((Join-Path $InstallDir ".venv\Scripts\python.exe"),
                (Join-Path $root ".venv\Scripts\python.exe"))
$python = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source }
}
if (-not $python) {
    Write-Host "Python was not found. Install Sentinel-FX first (Install.cmd)." -ForegroundColor Red
    exit 2
}
$script = Join-Path $root "scripts\mt5_check.py"
if (-not (Test-Path -LiteralPath $script)) { $script = Join-Path $InstallDir "scripts\mt5_check.py" }
$report = Join-Path $env:TEMP "mt5-check.html"

if (-not $Login) {
    $Login = Read-Host "MT5 account number (press Enter to use the account the terminal is logged in to)"
}
$argv = @($script, "--report", $report)
if ($Login) {
    if ($Login -notmatch '^\d{3,15}$') {
        Write-Host "The account number must be digits only." -ForegroundColor Red
        exit 2
    }
    if (-not $Server) {
        $Server = Read-Host "Server name exactly as the broker gave it (for example Alpari-MT5-Demo)"
    }
    $secure = Read-Host "Trading password (not shown)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $env:SENTINEL_MT5_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $argv += @("--login", $Login, "--server", $Server)
}
if ($Path) { $argv += @("--path", $Path) }
$env:PYTHONIOENCODING = "utf-8"
$code = 1
try {
    & $python @argv
    $code = $LASTEXITCODE
} finally {
    Remove-Item Env:\SENTINEL_MT5_PASSWORD -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $report) { Start-Process $report }
exit $code
