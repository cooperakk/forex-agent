#!/usr/bin/env bash
#
# Upgrade an existing installation in place.
#
#   sudo ./update.sh /path/to/sentinel-fx-1.1.0.tar.gz
#
# Backs up first, stops the engine, replaces the code (never the state),
# reinstalls dependencies, runs the tests, and restarts. If the tests fail it
# rolls the code back and restarts the old version.
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/sentinel-fx}"
STATE_DIR="${STATE_DIR:-/var/lib/sentinel}"
SENTINEL_USER="${SENTINEL_USER:-sentinel}"
ARCHIVE="${1:-}"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo." >&2; exit 1; }
[ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ] || {
    echo "usage: $0 /path/to/sentinel-fx-<version>.tar.gz" >&2; exit 1; }

echo "[update] backing up the current state first..."
"$INSTALL_DIR/deploy/backup.sh" "$STATE_DIR" "${BACKUP_DIR:-/var/backups/sentinel}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK="/opt/sentinel-fx.rollback-$STAMP"

echo "[update] stopping the engine..."
systemctl stop sentinel-engine sentinel-watchdog 2>/dev/null || true

echo "[update] keeping the old code at $ROLLBACK"
cp -a "$INSTALL_DIR" "$ROLLBACK"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$ARCHIVE" -C "$STAGE"
SRC="$(find "$STAGE" -maxdepth 1 -type d -name 'sentinel-fx*' | head -1)"
[ -n "$SRC" ] || SRC="$STAGE"

# --exclude var/ and .venv: state and the virtualenv survive the upgrade.
tar -C "$SRC" --exclude='./var' --exclude='./.venv' -cf - . | tar -C "$INSTALL_DIR" -xf -

cd "$INSTALL_DIR"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "[update] running the test suite before trusting this build..."
if ! .venv/bin/python -m pytest -q >/tmp/sentinel-update-tests.log 2>&1; then
    echo "[update] TESTS FAILED. Rolling back." >&2
    tail -30 /tmp/sentinel-update-tests.log >&2
    rm -rf "$INSTALL_DIR"
    mv "$ROLLBACK" "$INSTALL_DIR"
    systemctl start sentinel-engine sentinel-watchdog
    echo "[update] the previous version has been restored and restarted." >&2
    exit 1
fi
echo "[update] tests passed: $(tail -1 /tmp/sentinel-update-tests.log)"

if command -v npm >/dev/null 2>&1 && [ -f dashboard/package.json ]; then
    ( cd dashboard && npm ci --silent --no-audit --no-fund && npm run build --silent )
fi

chown -R "$SENTINEL_USER:$SENTINEL_USER" "$INSTALL_DIR"
install -m 644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start sentinel-engine sentinel-watchdog
sleep 4
"$INSTALL_DIR/deploy/scripts/healthcheck.sh" || true

echo
echo "[update] Done. Roll back manually if needed:"
echo "    sudo systemctl stop sentinel-engine sentinel-watchdog"
echo "    sudo rm -rf $INSTALL_DIR && sudo mv $ROLLBACK $INSTALL_DIR"
echo "    sudo systemctl start sentinel-engine sentinel-watchdog"
