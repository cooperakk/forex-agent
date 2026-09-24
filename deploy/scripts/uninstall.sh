#!/usr/bin/env bash
#
# Remove Sentinel-FX. Keeps the state directory unless you insist.
#
#   sudo ./uninstall.sh              # remove code and services, KEEP state
#   sudo ./uninstall.sh --purge      # remove everything, including the audit
#                                    # chain and the acceptance history
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/sentinel-fx}"
STATE_DIR="${STATE_DIR:-/var/lib/sentinel}"
CONF_DIR="${CONF_DIR:-/etc/sentinel}"
SENTINEL_USER="${SENTINEL_USER:-sentinel}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

[ "$(id -u)" -eq 0 ] || { echo "run with sudo." >&2; exit 1; }

echo "This will stop and remove the Sentinel-FX services and $INSTALL_DIR."
if [ "$PURGE" = "1" ]; then
    echo
    echo "  --purge ALSO deletes $STATE_DIR and $CONF_DIR."
    echo "  That includes the audit journal, every acceptance verdict, the"
    echo "  account store and the agent's memory. None of it can be recovered"
    echo "  from the broker. Take a backup first if there is any doubt."
fi
echo
read -r -p "Type REMOVE to continue: " CONFIRM
[ "$CONFIRM" = "REMOVE" ] || { echo "cancelled."; exit 1; }

systemctl disable --now sentinel-engine sentinel-watchdog sentinel-backup.timer \
    2>/dev/null || true
rm -f /etc/systemd/system/sentinel-*.service /etc/systemd/system/sentinel-*.timer
systemctl daemon-reload

rm -rf "$INSTALL_DIR"
echo "[uninstall] removed $INSTALL_DIR"

if [ "$PURGE" = "1" ]; then
    rm -rf "$STATE_DIR" "$CONF_DIR"
    userdel "$SENTINEL_USER" 2>/dev/null || true
    echo "[uninstall] removed the state, the configuration and the service user"
else
    echo "[uninstall] KEPT $STATE_DIR and $CONF_DIR"
    echo "[uninstall] delete them by hand, or re-run with --purge."
fi
