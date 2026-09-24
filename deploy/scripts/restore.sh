#!/usr/bin/env bash
#
# Restore state from a backup.
#
#   sudo ./restore.sh                        # newest backup
#   sudo ./restore.sh /path/to/backup.tar.gz
#
# Restores the WHOLE state directory, not individual files: the audit chain,
# the verdict registry, the account store and the agent state are consistent
# with each other only at snapshot time, and mixing eras produces a system
# whose drawdown ladder disagrees with its own trade history.
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/sentinel-fx}"
STATE_DIR="${STATE_DIR:-/var/lib/sentinel}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/sentinel}"
SENTINEL_USER="${SENTINEL_USER:-sentinel}"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo." >&2; exit 1; }

ARCHIVE="${1:-}"
if [ -z "$ARCHIVE" ]; then
    ARCHIVE=$(find "$BACKUP_DIR" -name 'sentinel-state-*.tar.gz' -printf '%T@ %p\n' \
              2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    [ -n "$ARCHIVE" ] || { echo "no backups found in $BACKUP_DIR" >&2; exit 1; }
    echo "[restore] newest backup: $ARCHIVE"
fi
[ -f "$ARCHIVE" ] || { echo "no such file: $ARCHIVE" >&2; exit 1; }

echo "[restore] verifying the archive BEFORE touching anything..."
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$ARCHIVE" -C "$STAGE"

if [ -f "$STAGE/audit.jsonl" ]; then
    "$INSTALL_DIR/.venv/bin/python" - "$STAGE/audit.jsonl" <<'PYV'
import sys
sys.path.insert(0, "/opt/sentinel-fx")
from sentinel.core.audit import AuditLog
ok, bad, msg = AuditLog(sys.argv[1]).verify()
print(f"[restore] audit chain in the backup: {'OK' if ok else 'BROKEN'} -- {msg}")
if not ok:
    print("[restore] refusing to restore a backup whose audit chain is broken.",
          file=sys.stderr)
    raise SystemExit(1)
PYV
fi

echo
echo "  This will REPLACE the contents of $STATE_DIR."
echo "  The current state will be moved aside, not deleted."
echo
read -r -p "  Type RESTORE to continue: " CONFIRM
[ "$CONFIRM" = "RESTORE" ] || { echo "[restore] cancelled."; exit 1; }

echo "[restore] stopping services..."
systemctl stop sentinel-engine sentinel-watchdog 2>/dev/null || true

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
if [ -d "$STATE_DIR/var" ]; then
    mv "$STATE_DIR/var" "$STATE_DIR/var.replaced-$STAMP"
    echo "[restore] previous state kept at $STATE_DIR/var.replaced-$STAMP"
fi
mkdir -p "$STATE_DIR/var"
cp -a "$STAGE/." "$STATE_DIR/var/"
[ -f "$STATE_DIR/var/config.json" ] && mv "$STATE_DIR/var/config.json" "$STATE_DIR/config.json"

chown -R "$SENTINEL_USER:$SENTINEL_USER" "$STATE_DIR"
chmod 700 "$STATE_DIR"
find "$STATE_DIR" -name 'users.db' -exec chmod 600 {} \; 2>/dev/null || true
find "$STATE_DIR" -name '*.jsonl' -exec chmod 600 {} \; 2>/dev/null || true

echo "[restore] starting services..."
systemctl start sentinel-engine sentinel-watchdog
sleep 4
"$INSTALL_DIR/deploy/scripts/healthcheck.sh" || true

cat <<EOF

[restore] Done.

  The restored state may be older than the broker's. The reconciler runs on
  startup and the venue is authoritative, so open positions and balances will
  be corrected automatically -- but CHECK the positions page before allowing
  new entries, and consider engaging the kill switch until you have.

EOF
