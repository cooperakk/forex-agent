# Sentinel-FX -- EMERGENCY STOP. No password, no network, no dashboard needed.
#
#   Double-click Kill-Switch.cmd, or:
#       powershell -ExecutionPolicy Bypass -File deploy\windows\Kill-Switch.ps1
#
# Creates the kill file. From the next decision cycle the engine opens NO new
# position. Open positions are not closed: their stops stay at the broker and
# the engine keeps managing them. Releasing the switch is deliberate and
# separate: the owner does it from the dashboard (with a second factor), or
# with -Release here.
param(
    [string]$StateDir = "C:\ProgramData\SentinelFX",
    [switch]$Release,
    [string]$Reason = "engaged from Kill-Switch.ps1"
)
$ErrorActionPreference = "Stop"
$kill = Join-Path $StateDir "var\KILL"
if ($Release) {
    if (Test-Path -LiteralPath $kill) {
        $answer = Read-Host "Type RELEASE to allow new positions again"
        if ($answer -ne "RELEASE") { Write-Host "Not released."; exit 1 }
        Remove-Item -LiteralPath $kill -Force
        Write-Host "Kill switch released. New positions are allowed again at the next cycle."
    } else {
        Write-Host "The kill switch is not engaged."
    }
    exit 0
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $kill) | Out-Null
$stamp = (Get-Date).ToUniversalTime().ToString("s") + "Z"
Set-Content -LiteralPath $kill -Value "$stamp $env:USERNAME $Reason" -Encoding ASCII
Write-Host ""
Write-Host "  KILL SWITCH ENGAGED ($kill)" -ForegroundColor Red
Write-Host "  No new positions will be opened. Open positions keep their broker-side stops."
Write-Host ""
