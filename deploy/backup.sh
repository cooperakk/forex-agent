#!/usr/bin/env bash
# Back up the four things whose loss cannot be recovered from the broker:
#   audit.jsonl   the hash-chained decision record
#   users.db      accounts, roles and TOTP secrets -- treat like a private key
#   verdicts.db   which strategies were ever allowed to trade, and why
#   agent_state.json  equity peak, period baselines, position metadata
#   memory.db     closed-trade history the learning loop reasons over
#
# Everything else -- prices, positions, balances -- can be re-fetched. These
# cannot. In particular, losing agent_state.json resets the drawdown ladder,
# so a system that was correctly trading at half size resumes at full size.
set -euo pipefail

STATE_DIR="${1:-/var/lib/sentinel}"
DEST_DIR="${2:-/var/backups/sentinel}"
KEEP_DAYS="${KEEP_DAYS:-30}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$DEST_DIR"
out="$DEST_DIR/sentinel-state-$stamp.tar.gz"

# SQLite files must not be copied while a write is in flight. `.backup` takes a
# consistent snapshot through the database's own locking; a plain `cp` of a
# live database is how you get a backup that restores into corruption.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for db in verdicts.db memory.db market.db calendar.db users.db ai.db brain.db macro.db; do
    src="$STATE_DIR/var/$db"
    [ -f "$src" ] || src="$STATE_DIR/$db"
    [ -f "$src" ] || continue
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$src" ".backup '$tmp/$db'"
    else
        python3 - "$src" "$tmp/$db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
PY
    fi
done

# mt5-intents.json resolves an order whose fate was UNKNOWN across a restart;
# the sealed credential stores are useless without their key, which is
# deliberately NOT copied here -- a backup that carries both the lock and the
# key protects nothing. Back up broker-secrets.key (or SENTINEL_SECRET_KEY)
# separately, somewhere else.
for f in audit.jsonl watchdog.jsonl admin.jsonl agent_state.json proposals.json config.json \
         brokers.json broker-secrets.json ai-secrets.json ai.json mt5-intents.json \
         licence.key licence-timing.json licence-lease.json notify.json notify-secrets.json; do
    for cand in "$STATE_DIR/var/$f" "$STATE_DIR/$f"; do
        [ -f "$cand" ] && cp -p "$cand" "$tmp/$f" && break
    done
done

tar -czf "$out" -C "$tmp" .
chmod 600 "$out"
echo "[backup] $out ($(du -h "$out" | cut -f1))"

# Verify the chain inside the backup rather than trusting that it copied.
# Deliberately a STANDALONE walker rather than AuditLog(): constructing an
# AuditLog opens the file for append and runs its recovery path, which would
# modify the very backup copy we are supposed to be checking. It also needs no
# import of the package, so it works from any install layout -- the previous
# version hard-coded /opt/sentinel-fx and did `except ImportError: exit(0)`,
# so anywhere else it silently reported nothing at all.
verify_chain() {
    python3 - "$1" <<'PYCHAIN'
import hashlib, json, sys

GENESIS = "0" * 64


def digest(rec):
    body = json.dumps({"seq": rec["seq"], "ts_ns": rec["ts_ns"],
                       "run_id": rec["run_id"], "event": rec["event"],
                       "actor": rec["actor"], "payload": rec["payload"],
                       "prev_hash": rec["prev_hash"]},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


prev, expected, n = GENESIS, 1, 0
with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as fh:
    for lineno, line in enumerate(fh, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            print("[backup] audit chain: BROKEN - line %d is not valid JSON" % lineno)
            sys.exit(1)
        if rec.get("seq") != expected:
            print("[backup] audit chain: BROKEN - expected seq %d, saw %r"
                  % (expected, rec.get("seq")))
            sys.exit(1)
        if rec.get("prev_hash") != prev:
            print("[backup] audit chain: BROKEN - previous-hash mismatch at seq %d" % expected)
            sys.exit(1)
        if digest(rec) != rec.get("hash"):
            print("[backup] audit chain: BROKEN - record hash mismatch at seq %d" % expected)
            sys.exit(1)
        prev, expected, n = rec["hash"], expected + 1, n + 1
print("[backup] audit chain: OK (%d records)" % n)
PYCHAIN
}

for journal in audit.jsonl watchdog.jsonl admin.jsonl; do
    if [ -f "$tmp/$journal" ]; then
        printf "[backup] %s: " "$journal"
        verify_chain "$tmp/$journal" || echo "[backup] WARNING: $journal did not verify"
    fi
done

find "$DEST_DIR" -name 'sentinel-state-*.tar.gz' -mtime "+$KEEP_DAYS" -delete
