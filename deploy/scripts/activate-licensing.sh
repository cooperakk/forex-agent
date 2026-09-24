#!/usr/bin/env bash
#
# Turn licence enforcement ON for this installation.
#
#   ./deploy/scripts/activate-licensing.sh
#
# Licensing ships INERT on purpose: with no vendor public key the gate reports
# `unlicensed_mode` and permits everything, which is correct for a self-hosted
# install where you are both the vendor and the customer. This script is the
# deliberate step that switches it on.
#
# It does four things:
#   1. generates the vendor keypair, if you do not already have one
#   2. signs the release integrity manifest
#   3. issues a licence for THIS machine
#   4. writes the public key into the service environment
#
# READ docs/LICENSING.md FIRST, especially the section on what this cannot do.
# Short version: it cannot stop someone with root on the machine. It stops
# casual copying, silent expiry, and tampering going unnoticed.
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STATE_DIR="${STATE_DIR:-/var/lib/sentinel}"
CONF_DIR="${CONF_DIR:-/etc/sentinel}"
KEY_DIR="${KEY_DIR:-$HOME/sentinel-vendor-keys}"
SENTINEL_USER="${SENTINEL_USER:-sentinel}"
TIER="${TIER:-unlimited}"
DAYS="${DAYS:-3650}"
LICENSEE="${LICENSEE:-$(hostname)}"

PY="$INSTALL_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

say() { printf '[licensing] %s\n' "$*"; }

say "install     $INSTALL_DIR"
say "key store   $KEY_DIR"
echo

# --- 1. keypair -------------------------------------------------------------
if [ -f "$KEY_DIR/private.pem" ]; then
    say "keypair     already exists (kept)"
else
    say "generating the vendor keypair..."
    "$PY" "$INSTALL_DIR/scripts/licensegen.py" keygen --out "$KEY_DIR" >/dev/null
    chmod 700 "$KEY_DIR"
    say "keypair     created"
fi
PUBKEY="$(cat "$KEY_DIR/public.txt")"

# --- 2. manifest ------------------------------------------------------------
say "signing the integrity manifest..."
VERSION="$(grep -m1 '^version' "$INSTALL_DIR/pyproject.toml" | cut -d'"' -f2)"
"$PY" "$INSTALL_DIR/scripts/licensegen.py" manifest \
    --key "$KEY_DIR/private.pem" --version "$VERSION" \
    --out "$INSTALL_DIR/MANIFEST.sig" >/dev/null
say "manifest    signed for version $VERSION"

# --- 3. licence for this machine -------------------------------------------
FP="$(mktemp)"; trap 'rm -f "$FP"' EXIT
"$PY" "$INSTALL_DIR/scripts/licensegen.py" fingerprint --out "$FP" >/dev/null 2>&1
"$PY" "$INSTALL_DIR/scripts/licensegen.py" issue \
    --key "$KEY_DIR/private.pem" --to "$LICENSEE" --tier "$TIER" \
    --days "$DAYS" --machine-file "$FP" \
    --out "$STATE_DIR/licence.key" >/dev/null 2>&1
chown "$SENTINEL_USER:$SENTINEL_USER" "$STATE_DIR/licence.key" 2>/dev/null || true
chmod 600 "$STATE_DIR/licence.key"
say "licence     issued: tier=$TIER, ${DAYS} days, bound to this machine"

# --- 4. environment ---------------------------------------------------------
ENV_FILE="$CONF_DIR/sentinel.env"
if [ -f "$ENV_FILE" ]; then
    if grep -q '^SENTINEL_LICENSE_PUBKEY=' "$ENV_FILE"; then
        sed -i "s|^SENTINEL_LICENSE_PUBKEY=.*|SENTINEL_LICENSE_PUBKEY=$PUBKEY|" "$ENV_FILE"
    else
        printf '\n# Licence enforcement, switched on by activate-licensing.sh\nSENTINEL_LICENSE_PUBKEY=%s\n' \
            "$PUBKEY" >> "$ENV_FILE"
    fi
    say "env         $ENV_FILE updated"
    if command -v systemctl >/dev/null 2>&1 \
       && systemctl is-active --quiet sentinel-engine; then
        systemctl restart sentinel-engine
        say "engine      restarted"
    fi
else
    say "no $ENV_FILE found. Add this line to your environment yourself:"
    echo
    echo "    SENTINEL_LICENSE_PUBKEY=$PUBKEY"
    echo
fi

cat <<EOF

  Licence enforcement is ON.

  BACK UP $KEY_DIR/private.pem, OFFLINE, NOW.
    - lose it  -> you can never issue or renew a licence again
    - leak it  -> every licence becomes forgeable, and the only remedy is a
                  new key and a re-issue of every licence in the field

  Check it took effect:
    $PY $INSTALL_DIR/scripts/licensegen.py inspect \\
        --licence $STATE_DIR/licence.key --pubkey $KEY_DIR/public.txt --check-machine

  Issue a licence for a customer:
    # they run:  licensegen.py fingerprint --out fp.json   and send you fp.json
    $PY $INSTALL_DIR/scripts/licensegen.py issue \\
        --key $KEY_DIR/private.pem --to "Customer Name" \\
        --tier live_single --days 365 --machine-file fp.json --out customer.key

  Every time you change the code, re-sign the manifest -- otherwise live
  trading will be refused because the files no longer match:
    $PY $INSTALL_DIR/scripts/licensegen.py manifest --key $KEY_DIR/private.pem

EOF
