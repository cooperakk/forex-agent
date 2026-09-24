#!/usr/bin/env bash
#
# Sentinel-FX -- one-command installer for Debian/Ubuntu.
#
#   sudo ./deploy/scripts/install.sh
#
# What it does, in order, stopping at the first failure:
#   1. checks the machine can actually run this (OS, RAM, disk, clock)
#   2. installs system packages
#   3. creates a dedicated unprivileged user and directories
#   4. builds a virtualenv and the dashboard
#   5. generates secrets and writes /etc/sentinel/sentinel.env
#   6. installs and starts the systemd units
#   7. creates the first owner account and prints where to find the enrolment
#
# It is IDEMPOTENT: running it again upgrades in place and never overwrites an
# existing secret, config or state directory. That matters more than it sounds,
# because the state directory holds the audit chain and the drawdown ladder's
# memory, and an installer that resets those is an installer that loses money.
set -Eeuo pipefail

SENTINEL_USER="${SENTINEL_USER:-sentinel}"
INSTALL_DIR="${INSTALL_DIR:-/opt/sentinel-fx}"
STATE_DIR="${STATE_DIR:-/var/lib/sentinel}"
CONF_DIR="${CONF_DIR:-/etc/sentinel}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/sentinel}"
LOG_PREFIX="[install]"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

say()  { printf '%s %s\n' "$LOG_PREFIX" "$*"; }
warn() { printf '%s \033[33mWARNING\033[0m %s\n' "$LOG_PREFIX" "$*" >&2; }
die()  { printf '%s \033[31mFAILED\033[0m %s\n' "$LOG_PREFIX" "$*" >&2; exit 1; }
step() { printf '\n%s \033[1m%s\033[0m\n' "$LOG_PREFIX" "$*"; }

trap 'die "aborted at line $LINENO. Nothing further was changed."' ERR

# --------------------------------------------------------------------------- #
# 1. preflight
# --------------------------------------------------------------------------- #
step "1/7  Checking this machine"

[ "$(id -u)" -eq 0 ] || die "run this with sudo."

. /etc/os-release 2>/dev/null || die "cannot identify the OS (/etc/os-release missing)."
case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) : ;;
    *) warn "this installer targets Debian/Ubuntu; '${ID:-unknown}' is untested." ;;
esac
say "OS            ${PRETTY_NAME:-unknown}"

ARCH="$(uname -m)"
say "architecture  $ARCH"
[ "$ARCH" = "x86_64" ] || [ "$ARCH" = "aarch64" ] \
    || warn "numpy/scipy wheels may not exist for $ARCH; the build may be slow."

MEM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
say "memory        ${MEM_MB} MB"
[ "$MEM_MB" -ge 900 ] || die "at least 1 GB of RAM is needed; this machine has ${MEM_MB} MB."
[ "$MEM_MB" -ge 1800 ] || warn "under 2 GB. The research lab will be slow and may be killed by the OOM killer."

DISK_MB=$(df -Pm /opt | awk 'NR==2 {print $4}')
say "free disk     ${DISK_MB} MB on /opt"
[ "$DISK_MB" -ge 2000 ] || die "at least 2 GB free is needed on /opt; there is ${DISK_MB} MB."

# The clock is not a nicety. Every decision timestamp, every idempotency key and
# every session-window check depends on it.
if command -v timedatectl >/dev/null 2>&1; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
        say "clock         synchronised"
    else
        warn "the clock is NOT synchronised with NTP."
        warn "Every decision timestamp and idempotency key depends on it."
        warn "Fix with:  timedatectl set-ntp true"
    fi
fi

if [ -d "$INSTALL_DIR" ]; then
    say "mode          UPGRADE (existing install at $INSTALL_DIR)"
    UPGRADE=1
else
    say "mode          FRESH INSTALL"
    UPGRADE=0
fi

# --------------------------------------------------------------------------- #
# 2. packages
# --------------------------------------------------------------------------- #
step "2/7  Installing system packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-dev build-essential \
    sqlite3 ca-certificates curl tar gzip >/dev/null
say "python        $(python3 --version 2>&1)"

if ! command -v node >/dev/null 2>&1; then
    warn "Node.js is not installed, so the dashboard cannot be built from source."
    if [ -d "$SRC_DIR/dashboard/dist" ]; then
        say "a prebuilt dashboard is present in the package; using it."
    else
        warn "install Node 20+ and re-run to get the dashboard."
    fi
else
    say "node          $(node --version)"
fi

# --------------------------------------------------------------------------- #
# 3. user and directories
# --------------------------------------------------------------------------- #
step "3/7  Creating the service user and directories"

if ! id -u "$SENTINEL_USER" >/dev/null 2>&1; then
    useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SENTINEL_USER"
    say "created user  $SENTINEL_USER"
else
    say "user          $SENTINEL_USER (exists)"
fi

mkdir -p "$INSTALL_DIR" "$STATE_DIR" "$CONF_DIR" "$BACKUP_DIR"
# 0700 on the state directory: it holds the audit chain, the account store with
# its TOTP secrets, and the verdict registry.
chmod 700 "$STATE_DIR" "$CONF_DIR" "$BACKUP_DIR"

# --------------------------------------------------------------------------- #
# 4. code, virtualenv, dashboard
# --------------------------------------------------------------------------- #
step "4/7  Installing the application"

if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
    # --exclude var/ so an upgrade never touches the running state.
    tar -C "$SRC_DIR" --exclude='./var' --exclude='./.git' \
        --exclude='./dashboard/node_modules' --exclude='**/__pycache__' \
        -cf - . | tar -C "$INSTALL_DIR" -xf -
    say "code copied to $INSTALL_DIR"
fi

cd "$INSTALL_DIR"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
    say "virtualenv created"
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
say "python dependencies installed"

if command -v npm >/dev/null 2>&1 && [ -f dashboard/package.json ]; then
    if [ ! -d dashboard/dist ] || [ "${REBUILD_DASHBOARD:-0}" = "1" ]; then
        say "building the dashboard (this takes a minute)..."
        ( cd dashboard && npm ci --silent --no-audit --no-fund && npm run build --silent )
        say "dashboard built"
    else
        say "dashboard     already built (REBUILD_DASHBOARD=1 to force)"
    fi
fi

# --------------------------------------------------------------------------- #
# 5. secrets and configuration
# --------------------------------------------------------------------------- #
step "5/7  Configuration and secrets"

ENV_FILE="$CONF_DIR/sentinel.env"
if [ ! -f "$ENV_FILE" ]; then
    JWT_SECRET="$("$INSTALL_DIR/.venv/bin/python" -c \
        'import secrets; print(secrets.token_urlsafe(48))')"
    ADMIN_PASS="$("$INSTALL_DIR/.venv/bin/python" -c \
        'import secrets; print(secrets.token_urlsafe(18))')"
    umask 077
    cat > "$ENV_FILE" <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
# This file contains secrets. It is 0600 and owned by root.

SENTINEL_JWT_SECRET=$JWT_SECRET

# Read ONCE, to create the first owner. DELETE these two lines afterwards --
# the account is stored in $STATE_DIR/users.db and survives restarts, and the
# environment cannot overwrite it.
SENTINEL_ADMIN_USER=owner
SENTINEL_ADMIN_PASSWORD=$ADMIN_PASS

# X-Forwarded-For is IGNORED unless a proxy is named here. Leave it unset for
# a loopback bind or an SSH tunnel.
#SENTINEL_TRUSTED_PROXY=127.0.0.1

# Licence file. Leave as-is unless the vendor told you otherwise.
SENTINEL_LICENSE=$STATE_DIR/licence.key

# --- MetaTrader 5 through the bridge ----------------------------------------
# The MetaTrader5 package is Windows-only. The terminal runs on a Windows
# machine with scripts/mt5_bridge.py and tunnels to this server; these two
# lines make the engine use it. Set them with:
#     sudo $INSTALL_DIR/deploy/scripts/connect-mt5.sh
SENTINEL_MT5_BRIDGE=
SENTINEL_MT5_BRIDGE_TOKEN=

# --- Broker credentials: fill these in before switching to live -------------
#OANDA_ACCOUNT_ID=
#OANDA_API_TOKEN=
#OANDA_API_HOST=https://api-fxpractice.oanda.com
EOF
    chmod 600 "$ENV_FILE"
    say "secrets generated -> $ENV_FILE"
    GENERATED_PASSWORD="$ADMIN_PASS"
else
    say "env file      $ENV_FILE (exists, left untouched)"
    GENERATED_PASSWORD=""
fi

CONFIG_FILE="$STATE_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    sudo -u "$SENTINEL_USER" SENTINEL_CONFIG="$CONFIG_FILE" \
        "$INSTALL_DIR/.venv/bin/python" - <<EOF
import sys
sys.path.insert(0, "$INSTALL_DIR")
sys.path.insert(0, "$INSTALL_DIR/scripts")
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("srv", "$INSTALL_DIR/scripts/serve.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
mod._default_config_for(Path("$CONFIG_FILE")).save("$CONFIG_FILE")
print("[install] default configuration written")
EOF
else
    say "config        $CONFIG_FILE (exists, left untouched)"
fi

chown -R "$SENTINEL_USER:$SENTINEL_USER" "$INSTALL_DIR" "$STATE_DIR" "$BACKUP_DIR"
chown root:"$SENTINEL_USER" "$ENV_FILE" && chmod 640 "$ENV_FILE"

# --------------------------------------------------------------------------- #
# 6. systemd
# --------------------------------------------------------------------------- #
step "6/7  Installing the services"

install -m 644 "$INSTALL_DIR/deploy/systemd/sentinel-engine.service"   /etc/systemd/system/
install -m 644 "$INSTALL_DIR/deploy/systemd/sentinel-watchdog.service" /etc/systemd/system/
install -m 644 "$INSTALL_DIR/deploy/systemd/sentinel-backup.service"   /etc/systemd/system/
install -m 644 "$INSTALL_DIR/deploy/systemd/sentinel-backup.timer"     /etc/systemd/system/
chmod +x "$INSTALL_DIR"/deploy/scripts/*.sh 2>/dev/null || true
systemctl daemon-reload

systemctl enable --now sentinel-engine.service   >/dev/null 2>&1
systemctl enable --now sentinel-watchdog.service >/dev/null 2>&1
systemctl enable --now sentinel-backup.timer     >/dev/null 2>&1
say "services enabled and started"

sleep 4
if ! systemctl is-active --quiet sentinel-engine; then
    warn "the engine is not running. The last 30 log lines:"
    journalctl -u sentinel-engine -n 30 --no-pager >&2 || true
    die "startup failed -- see above."
fi
say "engine        running"
systemctl is-active --quiet sentinel-watchdog \
    && say "watchdog      running" \
    || warn "the watchdog is not running; the dead-man switch is inactive."

# --------------------------------------------------------------------------- #
# 7. done
# --------------------------------------------------------------------------- #
step "7/7  Ready"

ENROL="$STATE_DIR/enrolment-owner.txt"
PORT="$(grep -oP '"bind_port"\s*:\s*\K[0-9]+' "$CONFIG_FILE" 2>/dev/null || echo 8088)"

cat <<EOF

  ============================================================
   Sentinel-FX is installed and running.
  ============================================================

  It starts in ADVISORY mode on the PAPER broker. It will not
  place a real order until you deliberately change that, and
  not even then without a passing acceptance run.

  1. Open the dashboard. It is bound to loopback, which is the
     safe default, so tunnel to it from your own computer:

         ssh -N -L ${PORT}:127.0.0.1:${PORT} $(whoami)@$(hostname -I 2>/dev/null | awk '{print $1}')

     then open  http://127.0.0.1:${PORT}

  2. Log in as:  owner
EOF

if [ -n "$GENERATED_PASSWORD" ]; then
cat <<EOF
     Password:  $GENERATED_PASSWORD

     ^ Written down now, shown ONCE. It is also in $ENV_FILE;
       delete SENTINEL_ADMIN_PASSWORD from there once you have
       logged in.
EOF
else
cat <<EOF
     (the password is the one from your existing $ENV_FILE)
EOF
fi

cat <<EOF

  3. Enrol your authenticator app from:

         sudo cat $ENROL

     Then delete that file. Every write action needs a code
     from it -- a stolen password alone cannot move money.

  4. To trade a MetaTrader broker (AMarkets, Alpari, ...): the
     terminal must run on a WINDOWS machine. There, run
         deploy/mt5-bridge/start-bridge.ps1   (prints a token once)
         deploy/mt5-bridge/tunnel.ps1         (keeps a tunnel to this server)
     and here:
         sudo $INSTALL_DIR/deploy/scripts/connect-mt5.sh
     Persian walkthrough: $INSTALL_DIR/docs/UBUNTU-FA.md

  Useful commands:
     systemctl status sentinel-engine
     journalctl -u sentinel-engine -f
     $INSTALL_DIR/deploy/scripts/healthcheck.sh
     $INSTALL_DIR/deploy/scripts/backup-now.sh

  STOP EVERYTHING immediately, at any time:
     sudo -u $SENTINEL_USER touch $STATE_DIR/var/KILL

EOF
