#!/usr/bin/env bash
# Take a backup right now, and say whether it is trustworthy.
set -Eeuo pipefail
INSTALL_DIR="${INSTALL_DIR:-/opt/sentinel-fx}"
STATE_DIR="${STATE_DIR:-/var/lib/sentinel}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/sentinel}"
exec "$INSTALL_DIR/deploy/backup.sh" "$STATE_DIR" "$BACKUP_DIR"
