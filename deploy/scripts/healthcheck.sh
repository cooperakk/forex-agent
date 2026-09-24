#!/usr/bin/env bash
#
# Is everything actually working? Run it any time; run it before you sleep.
#
#   ./healthcheck.sh            human output
#   ./healthcheck.sh --json     machine output, for monitoring
#
# Exit code: 0 all good, 1 warnings, 2 something is wrong.
set -uo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/sentinel-fx}"
STATE_DIR="${STATE_DIR:-/var/lib/sentinel}"
VAR_DIR="$STATE_DIR/var"
JSON=0
[ "${1:-}" = "--json" ] && JSON=1

FAIL=0; WARN=0
declare -a RESULTS

check() {           # check <name> <ok|warn|fail> <message>
    RESULTS+=("$1|$2|$3")
    case "$2" in
        fail) FAIL=$((FAIL+1)) ;;
        warn) WARN=$((WARN+1)) ;;
    esac
}

# --- services ---------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
    for unit in sentinel-engine sentinel-watchdog; do
        if systemctl is-active --quiet "$unit"; then
            since=$(systemctl show -p ActiveEnterTimestamp --value "$unit" 2>/dev/null)
            check "$unit" ok "running since ${since:-unknown}"
        elif systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            check "$unit" fail "enabled but NOT running"
        else
            check "$unit" warn "not installed"
        fi
    done
    # A unit stopped by StartLimitBurst is the specific state that means
    # "it crashed repeatedly and systemd gave up" -- which is what you want,
    # and which is silent unless somebody looks.
    if systemctl is-failed --quiet sentinel-engine 2>/dev/null; then
        check "engine-state" fail "the unit is in a FAILED state; it may have hit its restart limit"
    fi
fi

# --- the decision loop ------------------------------------------------------
# The heartbeat only advances when a CYCLE completes, so this is the check that
# distinguishes "the process is up" from "the system is working".
HB="$VAR_DIR/heartbeat.json"
if [ -f "$HB" ]; then
    AGE=$(python3 - "$HB" <<'PYHB' 2>/dev/null
import json, sys, time
try:
    data = json.load(open(sys.argv[1]))
    print(int(time.time() - data["ts_ns"] / 1e9))
except Exception:
    print(-1)
PYHB
)
    if [ "$AGE" -lt 0 ]; then
        check "decision-loop" fail "the heartbeat file is unreadable"
    elif [ "$AGE" -gt 120 ]; then
        check "decision-loop" fail "no cycle completed for ${AGE}s -- the loop is wedged"
    elif [ "$AGE" -gt 60 ]; then
        check "decision-loop" warn "last cycle ${AGE}s ago"
    else
        check "decision-loop" ok "last cycle ${AGE}s ago"
    fi
else
    check "decision-loop" warn "no heartbeat file yet"
fi

# --- kill switch ------------------------------------------------------------
if [ -f "$VAR_DIR/KILL" ]; then
    REASON=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('reason','?'))" \
             "$VAR_DIR/KILL" 2>/dev/null || echo "unreadable")
    check "kill-switch" warn "ENGAGED -- no new trades. Reason: $REASON"
else
    check "kill-switch" ok "clear"
fi

# --- audit chain ------------------------------------------------------------
for journal in audit watchdog admin; do
    FILE="$VAR_DIR/${journal}.jsonl"
    [ -f "$FILE" ] || continue
    OUT=$("$INSTALL_DIR/.venv/bin/python" - "$FILE" <<'PYAUD' 2>&1
import sys
sys.path.insert(0, "/opt/sentinel-fx")
try:
    from sentinel.core.audit import AuditLog
except ImportError:
    print("SKIP package not importable"); raise SystemExit(0)
ok, bad, msg = AuditLog(sys.argv[1]).verify()
print(("OK " if ok else "BROKEN ") + msg)
PYAUD
)
    case "$OUT" in
        OK*)     check "journal-$journal" ok "${OUT#OK }" ;;
        SKIP*)   check "journal-$journal" warn "${OUT#SKIP }" ;;
        *)       check "journal-$journal" fail "$OUT" ;;
    esac
done

# --- clock ------------------------------------------------------------------
if command -v timedatectl >/dev/null 2>&1; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
        check "clock" ok "synchronised"
    else
        check "clock" fail "NOT synchronised -- every timestamp and idempotency key is suspect"
    fi
fi

# --- disk -------------------------------------------------------------------
if [ -d "$STATE_DIR" ]; then
    PCT=$(df -P "$STATE_DIR" | awk 'NR==2 {gsub(/%/,""); print $5}')
    if [ "$PCT" -ge 95 ]; then
        check "disk" fail "${PCT}% full -- the audit journal cannot be written"
    elif [ "$PCT" -ge 85 ]; then
        check "disk" warn "${PCT}% full"
    else
        check "disk" ok "${PCT}% used"
    fi
fi

# --- backups ----------------------------------------------------------------
BACKUP_DIR="${BACKUP_DIR:-/var/backups/sentinel}"
if [ -d "$BACKUP_DIR" ]; then
    NEWEST=$(find "$BACKUP_DIR" -name 'sentinel-state-*.tar.gz' -printf '%T@\n' 2>/dev/null \
             | sort -rn | head -1)
    if [ -z "$NEWEST" ]; then
        check "backups" warn "no backup has ever been taken"
    else
        HOURS=$(( ($(date +%s) - ${NEWEST%.*}) / 3600 ))
        if [ "$HOURS" -gt 26 ]; then
            check "backups" fail "the newest backup is ${HOURS}h old"
        elif [ "$HOURS" -gt 12 ]; then
            check "backups" warn "the newest backup is ${HOURS}h old"
        else
            check "backups" ok "newest is ${HOURS}h old"
        fi
    fi
fi

# --- licence ----------------------------------------------------------------
if [ -f "$STATE_DIR/licence.key" ] && [ -n "${SENTINEL_LICENSE_PUBKEY:-}" ]; then
    OUT=$("$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/licensegen.py" \
          inspect --licence "$STATE_DIR/licence.key" --check-machine 2>&1 | head -1)
    case "$OUT" in
        VALID*) check "licence" ok "valid" ;;
        *)      check "licence" fail "$OUT" ;;
    esac
fi

# --- output -----------------------------------------------------------------
if [ "$JSON" = "1" ]; then
    printf '{"checks":['
    first=1
    for row in "${RESULTS[@]}"; do
        IFS='|' read -r name state msg <<< "$row"
        [ $first = 1 ] || printf ','
        first=0
        printf '{"name":"%s","state":"%s","message":"%s"}' "$name" "$state" "${msg//\"/\\\"}"
    done
    printf '],"failures":%d,"warnings":%d}\n' "$FAIL" "$WARN"
else
    echo
    echo "  Sentinel-FX health  --  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "  ------------------------------------------------------------"
    for row in "${RESULTS[@]}"; do
        IFS='|' read -r name state msg <<< "$row"
        case "$state" in
            ok)   mark=$'\033[32m  OK  \033[0m' ;;
            warn) mark=$'\033[33m WARN \033[0m' ;;
            fail) mark=$'\033[31m FAIL \033[0m' ;;
        esac
        printf '  [%s] %-16s %s\n' "$mark" "$name" "$msg"
    done
    echo "  ------------------------------------------------------------"
    if [ "$FAIL" -gt 0 ]; then
        echo "  $FAIL problem(s), $WARN warning(s)."
        echo "  Start with:  journalctl -u sentinel-engine -n 50 --no-pager"
    elif [ "$WARN" -gt 0 ]; then
        echo "  $WARN warning(s), nothing broken."
    else
        echo "  Everything is healthy."
    fi
    echo
fi

[ "$FAIL" -gt 0 ] && exit 2
[ "$WARN" -gt 0 ] && exit 1
exit 0
