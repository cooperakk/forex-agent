# Sentinel-FX -- keep an SSH tunnel from this Windows machine to the server.
#
#   .\tunnel.ps1 -Server 203.0.113.10            (user defaults to 'ubuntu')
#   .\tunnel.ps1 -Server 203.0.113.10 -User root
#
# The tunnel is REVERSE: the server's 127.0.0.1:5555 is forwarded to this
# machine's 127.0.0.1:5555, where start-bridge.ps1 is listening. Nothing is
# opened on the internet; the server reaches the terminal only through SSH,
# which is what encrypts the bridge traffic.
#
# The loop reconnects when the link drops. Keep this window open, or install
# it as a scheduled task at logon (see README-FA.md).
param(
    [Parameter(Mandatory = $true)] [string] $Server,
    [string] $User = "ubuntu",
    [int]    $Port = 5555,
    [int]    $SshPort = 22
)
$ErrorActionPreference = "Continue"

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "  The OpenSSH client is not installed."
    Write-Host "  Settings -> Apps -> Optional features -> Add -> 'OpenSSH Client'."
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "[tunnel] forwarding $Server:127.0.0.1:$Port  <-  this PC 127.0.0.1:$Port"
Write-Host "[tunnel] first connection asks for the server password (or uses your key)."
Write-Host "[tunnel] Ctrl+C stops it."
while ($true) {
    & ssh -p $SshPort -N `
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
        -o ExitOnForwardFailure=yes `
        -R "127.0.0.1:${Port}:127.0.0.1:${Port}" "$User@$Server"
    Write-Host "[tunnel] disconnected ($(Get-Date -Format 'HH:mm:ss')); retrying in 10 s"
    Start-Sleep -Seconds 10
}
