# Sentinel-FX -- shared helpers for the Windows scripts. Dot-sourced, not run.
#
# Compatible with Windows PowerShell 5.1 (the default on Windows Server 2016,
# 2019 and 2022) and PowerShell 7. Deliberately ASCII-only: Windows PowerShell
# 5.1 reads a .ps1 without a byte-order mark in the system ANSI code page, and a
# single non-ASCII character would be mis-decoded on a Persian or Chinese
# Windows install.

Set-StrictMode -Version 2.0

$script:DefaultInstallDir = "C:\SentinelFX\app"
$script:DefaultStateDir   = "C:\ProgramData\SentinelFX"
$script:TaskPath          = "\SentinelFX\"
$script:EngineTask        = "SentinelFX-Engine"
$script:WatchdogTask      = "SentinelFX-Watchdog"
$script:BackupTask        = "SentinelFX-Backup"
$script:MinPython         = [Version]"3.11"

function Say([string]$m)  { Write-Host "[sentinel] $m" }
function Warn([string]$m) { Write-Host "[sentinel] WARNING: $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "[sentinel] FAILED: $m" -ForegroundColor Red; exit 1 }
function Step([string]$m) { Write-Host ""; Write-Host "[sentinel] == $m ==" -ForegroundColor Cyan }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-PythonVersion([string]$exe, [string[]]$pre = @()) {
    try {
        $out = & $exe @pre -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return [Version]($out | Select-Object -First 1).Trim() }
    } catch { }
    return $null
}

# Returns @{ Exe = "...python.exe"; Version = [Version] } or $null. Prefers the
# py launcher (it knows every installed version), then python on PATH.
function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("3.13", "3.12", "3.11")) { $candidates += ,@("py", "-$v") }
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $candidates += ,@($cmd.Source) }
    }
    foreach ($c in $candidates) {
        $exe = $c[0]; $pre = @(); if ($c.Count -gt 1) { $pre = $c[1..($c.Count - 1)] }
        $ver = Get-PythonVersion $exe $pre
        if ($ver -and $ver -ge $script:MinPython) {
            $real = & $exe @pre -c "import sys; print(sys.executable)" 2>$null
            return @{ Exe = ($real | Select-Object -First 1).Trim(); Version = $ver }
        }
    }
    return $null
}

# KEY=VALUE lines, '#' comments. Values are taken verbatim (no quote parsing),
# exactly like systemd's EnvironmentFile for the values this project writes.
function Read-EnvFile([string]$path) {
    $vars = @{}
    if (-not (Test-Path -LiteralPath $path)) { return $vars }
    foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
        $t = $line.Trim()
        if ($t -eq "" -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        $vars[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1)
    }
    return $vars
}

function New-Secret([int]$bytes = 36) {
    $buf = New-Object byte[] $bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    return ([Convert]::ToBase64String($buf)).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

# Only SYSTEM, Administrators and (optionally) one account may read the state
# directory: it holds the audit chain, the account store with its TOTP
# secrets, the sealed credential stores and their key.
function Protect-Directory([string]$path, [string]$account = "") {
    $grants = @("*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")
    if ($account) { $grants += "$($account):(OI)(CI)M" }
    $icaclsArgs = @($path, "/inheritance:r", "/grant:r") + $grants
    & icacls.exe @icaclsArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { Warn "icacls could not restrict $path (exit $LASTEXITCODE)" }
}

# The CODE directory: Administrators and SYSTEM may change it, everyone else
# may only read and execute. Without this, a folder created under C:\ inherits
# "Authenticated Users: Modify" from the drive root, so any local account could
# edit Python that runs as SYSTEM (or as the trading user) -- a privilege
# escalation and a way around every control in the code.
function Protect-CodeDirectory([string]$path) {
    & icacls.exe $path /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" `
        "*S-1-5-32-545:(OI)(CI)RX" | Out-Null
    if ($LASTEXITCODE -ne 0) { Warn "icacls could not restrict $path (exit $LASTEXITCODE)" }
}

function Test-Signed([string]$file, [string]$publisherLike) {
    $sig = Get-AuthenticodeSignature -FilePath $file
    if ($sig.Status -ne "Valid") { return $false }
    return ($sig.SignerCertificate.Subject -like $publisherLike)
}

function Get-EngineProcess([string]$installDir) {
    $needle = ($installDir.TrimEnd("\") + "\").ToLower()
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($needle) -and
                       $_.CommandLine -like "*serve.py*" }
}

function Get-HealthStatus([int]$port = 8088) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 5
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}
