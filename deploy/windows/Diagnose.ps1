# Sentinel-FX -- Windows troubleshooter.
#
#   Double-click Diagnose.cmd, or:
#       powershell -ExecutionPolicy Bypass -File deploy\windows\Diagnose.ps1 [-Fix] [-Bundle]
#
# Answers, in order: is it installed, is it running, is it DECIDING, is its
# state intact, and is anything about to stop it.
#   -Fix     restarts stopped tasks and re-applies the directory permissions
#   -Bundle  writes a support zip WITHOUT secrets (no env file, no account
#            store, no credential stores, no licence, account ids redacted)
# Nothing here ever places, modifies or closes an order.
param(
    [string]$InstallDir = "C:\SentinelFX\app",
    [string]$StateDir   = "C:\ProgramData\SentinelFX",
    [int]$Port = 8088,
    [switch]$Fix,
    [switch]$Bundle
)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "common.ps1")

$script:problems = 0
$report = New-Object System.Collections.Generic.List[string]
function Sec([string]$m) { $report.Add(""); $report.Add("== $m =="); Write-Host ""; Write-Host "== $m ==" -ForegroundColor DarkGray }
function Ok([string]$m)   { $report.Add("  ok   $m"); Write-Host "   ok   $m" -ForegroundColor Green }
function W([string]$m)    { $report.Add("  warn $m"); Write-Host "   warn $m" -ForegroundColor Yellow }
function Bad([string]$m)  { $report.Add("  FAIL $m"); Write-Host "   FAIL $m" -ForegroundColor Red; $script:problems++ }
function Hint([string]$m) { $report.Add("       -> $m"); Write-Host "        -> $m" -ForegroundColor DarkGray }

$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
$var = Join-Path $StateDir "var"

Sec "1. Installation"
if (Test-Path -LiteralPath (Join-Path $InstallDir "sentinel")) { Ok "installed at $InstallDir" }
else { Bad "nothing at $InstallDir"; Hint "run deploy\windows\Install.cmd as Administrator"; exit 2 }
if (Test-Path -LiteralPath $python) { Ok "python $(Get-PythonVersion $python)" }
else { Bad "no virtualenv at $InstallDir\.venv"; Hint "re-run Install.cmd" }
& $python -c "import sys; sys.path.insert(0, r'$InstallDir'); import sentinel" 2>$null
if ($LASTEXITCODE -eq 0) { Ok "the package imports" } else { Bad "the package does not import"; Hint "$python -c `"import sentinel`"" }
if (Test-Path -LiteralPath (Join-Path $InstallDir "dashboard\dist\index.html")) { Ok "dashboard built" }
else { W "no dashboard build -- the API works, the console does not"; Hint "install Node.js 20 and re-run Install.cmd" }

Sec "2. Tasks and processes"
foreach ($t in @($script:EngineTask, $script:WatchdogTask, $script:BackupTask)) {
    $task = Get-ScheduledTask -TaskPath $script:TaskPath -TaskName $t -ErrorAction SilentlyContinue
    if (-not $task) { Bad "task $t is not registered"; Hint "re-run Install.cmd"; continue }
    $info = Get-ScheduledTaskInfo -TaskPath $script:TaskPath -TaskName $t
    $state = "$($task.State)"
    if ($t -ne $script:BackupTask -and $state -ne "Running") {
        Bad "task $t is $state (last result $($info.LastTaskResult))"
        if ($Fix) { Start-ScheduledTask -TaskPath $script:TaskPath -TaskName $t; Hint "started $t" }
        else { Hint "Start-ScheduledTask -TaskPath '$($script:TaskPath)' -TaskName '$t'   (or run with -Fix)" }
    } else {
        Ok "task $t is $state (last run $($info.LastRunTime))"
    }
}
$procs = @(Get-EngineProcess $InstallDir)
if ($procs.Count -eq 1) { Ok "engine process $($procs[0].ProcessId) is running" }
elseif ($procs.Count -gt 1) { Bad "$($procs.Count) engine processes are running -- only one may trade an account" }
else { Bad "no engine process is running" }

Sec "3. Is it serving and deciding?"
if (Get-HealthStatus $Port) { Ok "http://127.0.0.1:$Port/health answers" }
else { Bad "the API does not answer on port $Port"; Hint "read $StateDir\logs\engine.log" }
$hb = Join-Path $var "heartbeat.json"
if (Test-Path -LiteralPath $hb) {
    $age = ((Get-Date) - (Get-Item -LiteralPath $hb).LastWriteTime).TotalSeconds
    if ($age -lt 300) { Ok ("heartbeat {0:N0}s old" -f $age) }
    else { Bad ("heartbeat is {0:N0}s old -- the decision loop is not completing cycles" -f $age) }
} else { W "no heartbeat file yet" }
$kill = Join-Path $var "KILL"
if (Test-Path -LiteralPath $kill) { W "the KILL SWITCH is engaged: $((Get-Content -LiteralPath $kill -Raw).Trim())"; Hint "release it from the dashboard (owner + code), or Kill-Switch.ps1 -Release" }
else { Ok "kill switch not engaged" }
$stateFile = Join-Path $var "agent_state.json"
if (Test-Path -LiteralPath $stateFile) {
    try {
        $st = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        if ($st.halted) { W "the agent is HALTED: $($st.halt_reason)"; Hint "resolve the cause, then resume from the dashboard (owner)" }
        else { Ok "agent is not halted" }
    } catch { Bad "agent_state.json is unreadable -- the engine will refuse new risk until it is restored" }
}

Sec "4. State integrity"
$verifier = @'
import hashlib, json, sys
prev, expected = "0" * 64, 1
with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        body = json.dumps({k: rec[k] for k in ("seq", "ts_ns", "run_id", "event", "actor",
                                               "payload", "prev_hash")},
                          sort_keys=True, separators=(",", ":"))
        if rec["seq"] != expected or rec["prev_hash"] != prev or \
                hashlib.sha256(body.encode()).hexdigest() != rec["hash"]:
            print("BROKEN at seq %d" % expected); sys.exit(1)
        prev, expected = rec["hash"], expected + 1
print("intact (%d records)" % (expected - 1))
'@
$tmpPy = Join-Path $env:TEMP "sentinel-verify-chain.py"
Set-Content -LiteralPath $tmpPy -Value $verifier -Encoding ASCII
$audit = Join-Path $var "audit.jsonl"
if (Test-Path -LiteralPath $audit) {
    $out = & $python $tmpPy $audit 2>&1
    if ($LASTEXITCODE -eq 0) { Ok "audit chain $out" } else { Bad "audit chain $out"; Hint "the journal was edited or truncated; keep a copy and contact support" }
} else { W "no audit journal yet" }
Remove-Item -LiteralPath $tmpPy -Force -ErrorAction SilentlyContinue
$lic = & $python -c "import sys; sys.path.insert(0, r'$InstallDir'); from sentinel.licensing import LicenseGate; s = LicenseGate(licence_path=r'$var\licence.key', root=r'$InstallDir').check(); print('unlicensed build' if s.unlicensed_mode else ('valid' if s.valid else 'INVALID: ' + (s.headline or s.reason)))" 2>&1
if ("$lic" -like "INVALID*") { W "licence $lic" } else { Ok "licence: $lic" }
$drive = Get-PSDrive ((Split-Path -Qualifier $StateDir).TrimEnd(":")) -ErrorAction SilentlyContinue
if ($drive) {
    $freeMb = [int]($drive.Free / 1MB)
    if ($freeMb -gt 1000) { Ok "free disk for state: $freeMb MB" } else { Bad "only $freeMb MB free for state -- the engine halts when it cannot persist state" }
}
if ($Fix) {
    Protect-Directory $StateDir
    Protect-CodeDirectory $InstallDir
    Hint "directory permissions re-applied"
}

Sec "5. Environment"
$w32 = & w32tm /query /status 2>$null
if ($LASTEXITCODE -eq 0 -and ($w32 -match "Source")) { Ok "time service running" } else { Bad "the Windows Time service is not synchronising" }
if (Get-Process -Name terminal64 -ErrorAction SilentlyContinue) { Ok "a MetaTrader 5 terminal is running" }
else { W "no MetaTrader 5 terminal is running (needed only for MT5 brokers on this machine)" }
$log = Join-Path $StateDir "logs\engine.log"
if (Test-Path -LiteralPath $log) {
    $errs = @(Get-Content -LiteralPath $log -Tail 400 | Select-String -Pattern "Traceback|ERROR|refusing|FAILED")
    if ($errs.Count -gt 0) {
        W "$($errs.Count) error line(s) in the last 400 log lines; the most recent:"
        $errs | Select-Object -Last 3 | ForEach-Object { Hint $_.Line.Trim() }
    } else { Ok "no errors in the recent log" }
}

Sec "Summary"
if ($script:problems -eq 0) { Ok "no problems found" } else { Bad "$($script:problems) problem(s) found" }

if ($Bundle) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $dir = Join-Path $env:TEMP "sentinel-support-$stamp"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $report | Set-Content -LiteralPath (Join-Path $dir "diagnose.txt") -Encoding UTF8
    $cfgPath = Join-Path $StateDir "config.json"
    if (Test-Path -LiteralPath $cfgPath) {
        (Get-Content -LiteralPath $cfgPath -Raw) -replace '("expected_account_(id|server)"\s*:\s*)"[^"]*"', '$1"<redacted>"' |
            Set-Content -LiteralPath (Join-Path $dir "config.json") -Encoding UTF8
    }
    foreach ($name in @("engine.log", "watchdog.log")) {
        $src = Join-Path $StateDir "logs\$name"
        if (Test-Path -LiteralPath $src) { Get-Content -LiteralPath $src -Tail 2000 | Set-Content -LiteralPath (Join-Path $dir $name) -Encoding UTF8 }
    }
    if (Test-Path -LiteralPath $audit) { Get-Content -LiteralPath $audit -Tail 300 | Set-Content -LiteralPath (Join-Path $dir "audit-tail.jsonl") -Encoding UTF8 }
    if (Test-Path -LiteralPath $hb) { Copy-Item -LiteralPath $hb -Destination $dir }
    $zip = Join-Path ([Environment]::GetFolderPath("Desktop")) "sentinel-support-$stamp.zip"
    Compress-Archive -Path (Join-Path $dir "*") -DestinationPath $zip -Force
    Remove-Item -LiteralPath $dir -Recurse -Force
    Write-Host ""
    Write-Host "  Support bundle: $zip  (contains no passwords, keys or licence)" -ForegroundColor Cyan
}
if ($script:problems -gt 0) { exit 1 } else { exit 0 }
