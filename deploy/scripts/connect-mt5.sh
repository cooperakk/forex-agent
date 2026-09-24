#!/usr/bin/env bash
#
# Point the engine on this Ubuntu server at a MetaTrader terminal elsewhere.
#
#   sudo ./deploy/scripts/connect-mt5.sh            interactive: asks for the token
#   sudo ./deploy/scripts/connect-mt5.sh <token>    non-interactive
#   sudo ./deploy/scripts/connect-mt5.sh --test     only check the bridge
#   sudo ./deploy/scripts/connect-mt5.sh --off      stop using the bridge
#
# The terminal runs on a Windows machine with scripts/mt5_bridge.py, which
# prints a token once. That machine opens an SSH tunnel to this server so the
# bridge appears here on 127.0.0.1:5555. This script writes the two variables
# the engine reads, restarts it, and proves the account is reachable -- with a
# read-only probe that cannot place an order.
set -Eeuo pipefail

CONF_DIR="${CONF_DIR:-/etc/sentinel}"
INSTALL_DIR="${INSTALL_DIR:-/opt/sentinel-fx}"
STATE_DIR="${STATE_DIR:-/var/lib/sentinel}"
ENV_FILE="$CONF_DIR/sentinel.env"
ADDRESS="${SENTINEL_MT5_BRIDGE_ADDRESS:-127.0.0.1:5555}"
P="[connect-mt5]"

say()  { printf '%s %s\n' "$P" "$*"; }
die()  { printf '%s \033[31mFAILED\033[0m %s\n' "$P" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo."
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found; run install.sh first."

set_var() {   # set_var NAME VALUE  -- replace or append in the env file
    local name="$1" value="$2"
    if grep -q "^#\?${name}=" "$ENV_FILE"; then
        sed -i "s|^#\?${name}=.*|${name}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$name" "$value" >> "$ENV_FILE"
    fi
}

probe() {
    # Read-only: account_info and a tick. Nothing here can send an order.
    set -a; . "$ENV_FILE"; set +a
    "$INSTALL_DIR/.venv/bin/python" - <<'EOF'
import os, sys
sys.path.insert(0, os.environ.get("INSTALL_DIR", "/opt/sentinel-fx"))
from sentinel.brokers.mt5_bridge import bridge_from_env, BridgeError
try:
    m = bridge_from_env()
except BridgeError as exc:
    print(f"  bridge: {exc}"); sys.exit(2)
if m is None:
    print("  bridge: not configured"); sys.exit(2)
try:
    if not m.initialize():
        print(f"  bridge: reachable, but the terminal refused initialize(): {m.last_error()}")
        sys.exit(2)
    a = m.account_info()
    if a is None:
        print("  bridge: reachable, but the terminal is not signed in"); sys.exit(2)
    kind = {0: "DEMO", 1: "CONTEST (demo)", 2: "LIVE"}.get(int(getattr(a, "trade_mode", 0) or 0), "?")
    print(f"  account   {a.login}  {getattr(a, 'company', '')}  {getattr(a, 'server', '')}")
    print(f"  type      {kind}")
    print(f"  currency  {a.currency}   balance {a.balance}   leverage 1:{getattr(a, 'leverage', '?')}")
    syms = m.symbols_get() or []
    print(f"  symbols   {len(syms)} visible in the terminal")
    for name in ("EURUSD", "EURUSD.m", "EURUSDm"):
        t = m.symbol_info_tick(name)
        if t is not None:
            print(f"  tick      {name} bid {t.bid} ask {t.ask}")
            break
    m.shutdown()
except BridgeError as exc:
    print(f"  bridge: {exc}"); sys.exit(2)
print("  OK: the engine can see this account. The probe placed no order.")
EOF
}

case "${1:-}" in
    --off)
        set_var SENTINEL_MT5_BRIDGE ""
        set_var SENTINEL_MT5_BRIDGE_TOKEN ""
        systemctl restart sentinel-engine
        say "bridge disabled; the engine will use the local package if any, else paper."
        exit 0 ;;
    --test)
        say "testing the bridge at $(grep -oP '^SENTINEL_MT5_BRIDGE=\K.*' "$ENV_FILE" || echo unset)"
        INSTALL_DIR="$INSTALL_DIR" probe ;;
    "")
        printf '%s paste the token printed by mt5_bridge.py on the Windows machine: ' "$P"
        read -r TOKEN
        [ "${#TOKEN}" -ge 16 ] || die "that does not look like a bridge token."
        ;;
    *)  TOKEN="$1" ;;
esac

if [ -n "${TOKEN:-}" ]; then
    # Is the tunnel up? Refuse to configure a bridge nothing is listening on:
    # the engine would start, fail to build the broker, and sit in paper mode
    # with a confusing message.
    if ! timeout 3 bash -c "exec 3<>/dev/tcp/${ADDRESS%%:*}/${ADDRESS##*:}" 2>/dev/null; then
        die "nothing is listening on $ADDRESS. On the Windows machine start
        deploy\\mt5-bridge\\start-bridge.ps1 and deploy\\mt5-bridge\\tunnel.ps1 first."
    fi
    umask 077
    set_var SENTINEL_MT5_BRIDGE "$ADDRESS"
    set_var SENTINEL_MT5_BRIDGE_TOKEN "$TOKEN"
    chown root:sentinel "$ENV_FILE"; chmod 640 "$ENV_FILE"
    say "bridge configured at $ADDRESS"
    say "checking the account through the bridge (read-only)..."
    INSTALL_DIR="$INSTALL_DIR" probe || die "the bridge answered but the check failed; see above."
    systemctl restart sentinel-engine
    sleep 3
    systemctl is-active --quiet sentinel-engine || {
        journalctl -u sentinel-engine -n 20 --no-pager >&2 || true
        die "the engine did not come back up; see the log above."
    }
    say "engine restarted. Now open the dashboard -> 'بروکر و اتصال' -> جست‌وجو -> تست -> فعال‌سازی."
fi
