# Sentinel-FX on Windows Server

Windows is the natural home for MetaTrader 5: the `MetaTrader5` Python package
is Windows-only and attaches to a terminal running on the same machine, so on
Windows the engine talks to MT5 **directly**, without the bridge and SSH tunnel
that a Linux server needs.

Supported: Windows Server 2016, 2019, 2022, 2025 and Windows 10/11, 64-bit.
The scripts run on the built-in Windows PowerShell 5.1 (and on PowerShell 7).

## 1. Check the machine first

```
deploy\windows\Check-Environment.cmd
```

Read-only. It checks the Windows version, RAM, disk, **time synchronisation**,
Python/Node, MetaTrader 5, the dashboard port, and outbound HTTPS to PyPI, the
economic calendar, the central-bank feeds and the AI providers. Exit code 0 =
ready, 1 = ready with warnings, 2 = must fix first.

The clock matters more than it looks: every decision timestamp, idempotency key
and session window depends on it. If the check reports a free-running clock:

```powershell
w32tm /config /syncfromflags:manual /manualpeerlist:time.windows.com /update
w32tm /resync
```

## 2. Install

Right-click `deploy\windows\Install.cmd` -> **Run as administrator**.

| Option | Meaning |
|---|---|
| `-RunAs User` (default) | The engine runs in **your logon session**. Required for MetaTrader 5, whose terminal is a desktop program. On a VPS, configure automatic logon for a dedicated Windows user. |
| `-RunAs Service` | The engine starts at boot as SYSTEM, without a logon. For the paper simulator, OANDA, or MT5 through a bridge on another machine. |
| `-InstallDir`, `-StateDir`, `-Port` | Defaults: `C:\SentinelFX\app`, `C:\ProgramData\SentinelFX`, `8088`. |

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\Install.ps1 -RunAs User
```

What the installer does:

1. Checks the machine and refuses to continue on a hard failure.
2. Finds Python 3.11+ or installs Python 3.12 (winget, or the python.org
   installer **verified by its Authenticode signature**). Installs Node.js 20
   LTS the same way if the dashboard has to be built.
3. Mirrors the code to the install directory, builds a virtualenv, installs the
   pinned dependencies plus `MetaTrader5`, builds the dashboard.
4. **Locks the code directory** to Administrators and SYSTEM (everyone else:
   read and execute). A folder under `C:\` otherwise inherits
   "Authenticated Users: Modify", which would let any local account edit code
   that runs as SYSTEM.
5. Generates secrets into `sentinel.env`, writes the default configuration, and
   **locks the state directory** to SYSTEM, Administrators and the run-as user.
6. Registers three scheduled tasks under `\SentinelFX\`: the engine, the
   dead-man watchdog and a daily verified backup; starts them and waits for
   `http://127.0.0.1:8088/health`.

Re-running it upgrades in place and never overwrites an existing secret,
configuration or state.

### The engine supervisor

`run-engine.ps1` restarts the engine if it exits, with the same brake systemd
has on Linux: **five exits inside ten minutes and it stops** and waits for a
human. An engine that crashes on every start for a reason nobody can see must
not be restarted forever, because each restart is another chance to act on an
order whose fate is unknown. Logs: `C:\ProgramData\SentinelFX\logs\engine.log`
(rotated at 20 MB).

Windows has no SIGTERM, so stopping the task ends the process abruptly. The
engine is designed to survive that: its risk state is fsync'd, order ids are
deterministic, and on start it replays its journal and reconciles with the
broker before doing anything else.

## 3. First login

The dashboard is bound to **127.0.0.1** on purpose. Do not open the port in the
firewall. Either open `http://127.0.0.1:8088` in a browser on the server (RDP),
or tunnel from your own computer:

```
ssh -N -L 8088:127.0.0.1:8088 user@your-server
```

Log in as `owner` with the password printed by the installer, enrol your
authenticator app from `C:\ProgramData\SentinelFX\var\enrolment-owner.txt`,
then **delete that file** and the `SENTINEL_ADMIN_PASSWORD` line in
`sentinel.env`.

## 4. Day-to-day

| Task | Command |
|---|---|
| Emergency stop (no password, no network) | `deploy\windows\Kill-Switch.cmd` |
| Release the emergency stop | dashboard (owner + code), or `Kill-Switch.ps1 -Release` |
| Health check / troubleshooting | `deploy\windows\Diagnose.cmd` |
| Repair stopped tasks and permissions | `Diagnose.ps1 -Fix` |
| Support bundle without secrets | `Diagnose.ps1 -Bundle` (written to the Desktop) |
| Backup now | `deploy\windows\Backup.ps1` |
| Upgrade (backup, install, rollback on failure) | `Update.ps1 -Source C:\path\to\sentinel-fx-1.5.0.zip` |
| Uninstall (state is kept) | `Uninstall.ps1`   (`-PurgeState` deletes it after typing DELETE) |

The kill switch stops **new** positions only. Open positions keep their
broker-side stops and continue to be managed.

## 5. Backups

The daily task runs `scripts\backup_state.py`: SQLite databases are copied
through the database's own backup API, the audit chains are verified inside the
copy, and archives older than 30 days are pruned. The **credential key**
(`var\broker-secrets.key`) is deliberately **not** included -- a backup that
carries both the sealed credentials and their key protects nothing. Keep a copy
of the key somewhere else.
