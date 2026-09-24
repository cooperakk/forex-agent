#!/usr/bin/env bash
#
# Create an isolated account on this Ubuntu server and start its engine.
#
#   sudo ./deploy/scripts/add-account.sh alpari-demo --broker alpari --account 12345678 \
#        --server "Alpari-MT5-Demo" --port 8091 --bridge 127.0.0.1:5556
#
# Every argument after the name is passed to scripts/accounts.py add. The
# bridge token is asked for interactively (it is never a command-line argument).
# A second account needs a SECOND MetaTrader terminal on the Windows machine, a
# second mt5_bridge.py on another port, and a second tunnel to that port.
set -Eeuo pipefail
INSTALL_DIR="${INSTALL_DIR:-/opt/sentinel-fx}"
ROOT="${ACCOUNTS_ROOT:-/var/lib/sentinel/accounts}"
GROUP="${GROUP_LEDGER:-/var/lib/sentinel/group}"
P="[add-account]"
[ "$(id -u)" -eq 0 ] || { echo "$P run with sudo." >&2; exit 1; }
[ "$#" -ge 1 ] || { echo "usage: add-account.sh NAME --broker alpari --account N --server S --port 8091 --bridge 127.0.0.1:5556" >&2; exit 1; }
NAME="$1"; shift
mkdir -p "$ROOT" "$GROUP"
chown sentinel:sentinel "$ROOT" "$GROUP"
chmod 700 "$ROOT" "$GROUP"
runuser -u sentinel -- "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/accounts.py" \
    --root "$ROOT" add "$NAME" --group-ledger "$GROUP" "$@"
install -m 644 "$INSTALL_DIR/deploy/systemd/sentinel-account@.service" /etc/systemd/system/
install -m 644 "$INSTALL_DIR/deploy/systemd/sentinel-account-watchdog@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "sentinel-account@$NAME.service" "sentinel-account-watchdog@$NAME.service"
sleep 4
if systemctl is-active --quiet "sentinel-account@$NAME.service"; then
    echo "$P engine for $NAME is running."
else
    journalctl -u "sentinel-account@$NAME" -n 20 --no-pager >&2 || true
    echo "$P the engine did not start; see the log above." >&2
    exit 1
fi
PORT=$(grep -oP '"bind_port"\s*:\s*\K[0-9]+' "$ROOT/$NAME/config.json")
echo "$P dashboard: ssh -N -L ${PORT}:127.0.0.1:${PORT} <user>@<server>  then http://127.0.0.1:${PORT}"
echo "$P first owner password: $ROOT/$NAME/environment.json (SENTINEL_ADMIN_PASSWORD)"
echo "$P authenticator: sudo cat $ROOT/$NAME/var/enrolment-owner.txt   (then delete it)"
echo "$P all accounts: $ROOT/accounts.html"
