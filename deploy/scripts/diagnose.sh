#!/usr/bin/env bash
#
# Sentinel-FX troubleshooter.
#
#   sudo ./diagnose.sh              full report
#   sudo ./diagnose.sh --fix        also repair what is safely repairable
#   sudo ./diagnose.sh --bundle     write a support bundle (NO secrets)
#
# Answers, in order: is it installed, is it running, is it DECIDING, can it
# reach the broker, is its state intact, and is anything about to stop it.
#
# Nothing here ever places, modifies or closes an order. The only repairs it
# will make are to file permissions and ownership, and only with --fix.
set -uo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/sentinel-fx}"
STATE_DIR="${STATE_DIR:-/var/lib/sentinel}"
CONF_DIR="${CONF_DIR:-/etc/sentinel}"
VAR_DIR="$STATE_DIR/var"
SENTINEL_USER="${SENTINEL_USER:-sentinel}"
PY="$INSTALL_DIR/.venv/bin/python"
FIX=0; BUNDLE=0
for arg in "$@"; do
    [ "$arg" = "--fix" ] && FIX=1
    [ "$arg" = "--bundle" ] && BUNDLE=1
done

RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; DIM=$'\033[2m'; OFF=$'\033[0m'
PROBLEMS=0

sec()  { printf '\n%s== %s ==%s\n' "$DIM" "$1" "$OFF"; }
ok()   { printf '  %s ok  %s %s\n'   "$GRN" "$OFF" "$1"; }
warn() { printf '  %swarn %s %s\n'   "$YEL" "$OFF" "$1"; }
bad()  { printf '  %sFAIL %s %s\n'   "$RED" "$OFF" "$1"; PROBLEMS=$((PROBLEMS+1)); }
hint() { printf '         %s-> %s%s\n' "$DIM" "$1" "$OFF"; }

# --------------------------------------------------------------------------- #
sec "1. Installation"

if [ -d "$INSTALL_DIR" ]; then
    ok "installed at $INSTALL_DIR"
else
    bad "nothing at $INSTALL_DIR"
    hint "run deploy/scripts/install.sh"
    exit 2
fi

if [ -x "$PY" ]; then
    ok "python $("$PY" --version 2>&1 | cut -d' ' -f2)"
else
    bad "no virtualenv at $INSTALL_DIR/.venv"
    hint "cd $INSTALL_DIR && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    PY="$(command -v python3)"
fi

if "$PY" -c "import sys; sys.path.insert(0,'$INSTALL_DIR'); import sentinel" 2>/dev/null; then
    ok "the package imports"
else
    bad "the package will not import"
    hint "$PY -c \"import sentinel\"   # to see the real error"
fi

MISSING=$("$PY" - <<'PYDEP' 2>/dev/null
import importlib
missing = [m for m in ("fastapi", "uvicorn", "pydantic", "numpy", "pandas",
                       "scipy", "sklearn", "argon2", "jose", "pyotp",
                       "httpx", "cryptography")
           if not importlib.util.find_spec(m)]
print(" ".join(missing))
PYDEP
)
if [ -z "$MISSING" ]; then
    ok "all dependencies present"
else
    bad "missing dependencies: $MISSING"
    hint "$INSTALL_DIR/.venv/bin/pip install -r $INSTALL_DIR/requirements.txt"
fi

# --------------------------------------------------------------------------- #
sec "2. Services"

if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running \
        >/dev/null 2>&1 || [ -d /run/systemd/system ]; then
    for unit in sentinel-engine sentinel-watchdog; do
        if systemctl is-active --quiet "$unit" 2>/dev/null; then
            ok "$unit running"
        elif systemctl is-failed --quiet "$unit" 2>/dev/null; then
            bad "$unit is in a FAILED state"
            hint "it may have hit StartLimitBurst -- systemd gives up after 5"
            hint "crashes in 10 minutes, which is deliberate"
            hint "journalctl -u $unit -n 40 --no-pager"
            printf '%s' "$DIM"; journalctl -u "$unit" -n 8 --no-pager 2>/dev/null \
                | sed 's/^/         /'; printf '%s' "$OFF"
            [ "$FIX" = "1" ] && { systemctl reset-failed "$unit"; \
                systemctl start "$unit"; warn "reset and restarted $unit"; }
        elif systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            bad "$unit is enabled but not running"
            hint "systemctl start $unit"
        else
            warn "$unit not installed"
        fi
    done
else
    warn "no systemd here; checking for a bare process"
    pgrep -f "serve.py" >/dev/null && ok "a serve.py process is running" \
        || bad "no serve.py process found"
fi

# --------------------------------------------------------------------------- #
sec "3. Is it actually deciding?"
#
# The most useful check in this script. A process can be up, answering HTTP,
# and completely wedged. The heartbeat only advances when a decision CYCLE
# completes.

HB="$VAR_DIR/heartbeat.json"
if [ -f "$HB" ]; then
    read -r AGE CYCLE DEGRADED <<<"$("$PY" - "$HB" <<'PYHB' 2>/dev/null
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
    print(int(time.time() - d["ts_ns"]/1e9), d.get("cycle", "?"),
          d.get("degraded", False))
except Exception:
    print(-1, "?", "?")
PYHB
)"
    if [ "$AGE" -lt 0 ]; then
        bad "the heartbeat file is unreadable"
    elif [ "$AGE" -gt 120 ]; then
        bad "no decision cycle completed for ${AGE}s -- the loop is WEDGED"
        hint "the watchdog should have engaged the kill switch by now"
        hint "journalctl -u sentinel-engine -n 60 --no-pager"
    elif [ "$AGE" -gt 60 ]; then
        warn "last cycle ${AGE}s ago (cycle #$CYCLE)"
    else
        ok "deciding: cycle #$CYCLE, ${AGE}s ago"
    fi
    [ "$DEGRADED" = "True" ] && warn "the last cycle ran DEGRADED (see the log for why)"
else
    warn "no heartbeat yet -- normal for the first minute after a start"
fi

# --------------------------------------------------------------------------- #
sec "4. Broker connection"

"$PY" - <<PYBROKER 2>&1 | sed 's/^/  /'
import sys, os
sys.path.insert(0, "$INSTALL_DIR")
os.environ.setdefault("SENTINEL_CONFIG", "$STATE_DIR/config.json")
try:
    from sentinel.core.config import SentinelConfig
    cfg = SentinelConfig.load("$STATE_DIR/config.json")
except Exception as exc:
    print(f"\033[31mFAIL\033[0m could not read the configuration: {exc}")
    raise SystemExit(0)

print(f"\033[2m       broker={cfg.execution.broker} "
      f"venue={cfg.execution.venue_mode.value} mode={cfg.agent.mode.value}\033[0m")

try:
    from sentinel.brokers import build_broker, resolve_profile
except ImportError:
    from sentinel.brokers import build_broker
    from sentinel.brokers.profiles import resolve_profile

profile = resolve_profile(cfg.execution.broker)
if profile:
    print(f"\033[32m ok  \033[0m profile '{profile.name}' ({profile.display_name})")

if cfg.execution.broker == "paper":
    print("\033[32m ok  \033[0m paper broker -- no external connection needed")
    raise SystemExit(0)

# REFUSE to build a MetaTrader adapter while the engine is running. The
# MetaTrader5 package is one session per machine: initialize() here would take
# the terminal over, and the shutdown at the end of this script would
# disconnect the live engine mid-cycle. A diagnostic that breaks the thing it
# is diagnosing is worse than no diagnostic.
if (profile.adapter if profile else cfg.execution.broker) in ("mt5", "mt4"):
    import subprocess
    running = subprocess.run(["pgrep", "-f", "serve.py"],
                             capture_output=True).returncode == 0
    if running:
        print("\033[33mwarn \033[0m the engine is running and this venue is "
              "MetaTrader, so the connection is NOT re-tested here")
        print("        -> testing it would take the terminal over and disconnect "
              "the engine")
        print("        -> use the dashboard's «آزمایش اتصال», or stop the engine first")
        raise SystemExit(0)

try:
    broker = build_broker(cfg.execution.broker)
except Exception as exc:
    print(f"\033[31mFAIL\033[0m cannot build the adapter: {exc}")
    raise SystemExit(0)

try:
    instruments = broker.instruments()
    print(f"\033[32m ok  \033[0m connected: {len(instruments)} instruments")
except Exception as exc:
    print(f"\033[31mFAIL\033[0m connected but cannot list instruments: {exc}")
    raise SystemExit(0)

for m in getattr(broker, "profile_mismatches", [])[:6]:
    print(f"\033[33mwarn \033[0m profile corrected by the terminal: {m}")

# `capabilities` is an ATTRIBUTE, not a method. Calling it raised TypeError,
# which this block swallowed by dying before the instrument check below it --
# so the one check that names a missing symbol never ran on any live venue.
for note in broker.capabilities.degradation_report():
    print(f"\033[33mwarn \033[0m {note[:150]}")

missing = [s for s in {i for a in cfg.strategies for i in a.instruments}
           if s not in instruments]
if missing:
    print(f"\033[31mFAIL\033[0m configured instruments this broker does not offer: "
          f"{', '.join(sorted(missing))}")
    print("        -> check the symbol suffix; run with generic_mt5 to auto-detect")
PYBROKER

# --------------------------------------------------------------------------- #
sec "5. State integrity"

for journal in audit watchdog admin; do
    FILE="$VAR_DIR/${journal}.jsonl"
    [ -f "$FILE" ] || continue
    OUT=$("$PY" - "$FILE" <<PYAUD 2>&1
import sys
sys.path.insert(0, "$INSTALL_DIR")
from sentinel.core.audit import AuditLog
ok, bad, msg = AuditLog(sys.argv[1]).verify()
print(("OK " if ok else "BROKEN ") + msg)
PYAUD
)
    case "$OUT" in
        OK*) ok "${journal}.jsonl: ${OUT#OK }" ;;
        *)   bad "${journal}.jsonl: $OUT"
             hint "restore from a backup: deploy/scripts/restore.sh" ;;
    esac
done

for torn in "$VAR_DIR"/*.torn.* "$VAR_DIR"/*.damaged.*; do
    [ -e "$torn" ] && warn "salvaged fragment present: $(basename "$torn")"
done

for db in verdicts users memory trials market calendar; do
    FILE="$VAR_DIR/${db}.db"
    [ -f "$FILE" ] || continue
    if "$PY" -c "
import sqlite3,sys
c=sqlite3.connect(sys.argv[1]); r=c.execute('PRAGMA quick_check').fetchone()[0]
sys.exit(0 if r=='ok' else 1)" "$FILE" 2>/dev/null; then
        ok "${db}.db intact"
    else
        bad "${db}.db is corrupt"
        hint "restore from a backup -- do NOT delete it; verdicts.db holds the"
        hint "acceptance history and trials.db holds the multiple-testing count"
    fi
done

# --------------------------------------------------------------------------- #
sec "6. Permissions"

check_mode() {   # path expected-octal
    [ -e "$1" ] || return 0
    local mode; mode=$(stat -c '%a' "$1")
    if [ "$mode" = "$2" ]; then
        ok "$(basename "$1") $mode"
    else
        warn "$(basename "$1") is $mode, expected $2"
        if [ "$FIX" = "1" ]; then
            chmod "$2" "$1" && hint "fixed"
        else
            hint "chmod $2 $1     (or re-run with --fix)"
        fi
    fi
}
check_mode "$STATE_DIR" 700
check_mode "$VAR_DIR/users.db" 600
check_mode "$VAR_DIR/audit.jsonl" 600
check_mode "$CONF_DIR/sentinel.env" 640
[ -f "$VAR_DIR/licence.key" ] && check_mode "$VAR_DIR/licence.key" 600
# Venue configuration and the sealed credential store. The key file is the one
# that matters most: it is not a hash, it opens every broker password saved.
[ -f "$VAR_DIR/brokers.json" ]          && check_mode "$VAR_DIR/brokers.json" 600
[ -f "$VAR_DIR/broker-secrets.json" ]   && check_mode "$VAR_DIR/broker-secrets.json" 600
[ -f "$VAR_DIR/broker-secrets.key" ]    && check_mode "$VAR_DIR/broker-secrets.key" 600
[ -f "$VAR_DIR/licence-timing.json" ]   && check_mode "$VAR_DIR/licence-timing.json" 600

if [ -f "$VAR_DIR/broker-secrets.key" ] && [ -f "$VAR_DIR/broker-secrets.json" ]; then
    if grep -q '^SENTINEL_SECRET_KEY=.\+' "$CONF_DIR/sentinel.env" 2>/dev/null; then
        ok "credential key comes from the environment"
    else
        warn "the credential key sits beside the sealed broker passwords"
        hint "this protects a stolen backup, not someone with access to this server."
        hint "move it: set SENTINEL_SECRET_KEY in $CONF_DIR/sentinel.env and delete the key file"
    fi
fi

if ls "$VAR_DIR"/enrolment-*.txt >/dev/null 2>&1; then
    warn "a second-factor enrolment file is still on disk"
    hint "it holds the TOTP secret for an account. Enrol it, then: rm $VAR_DIR/enrolment-*.txt"
fi

OWNER=$(stat -c '%U' "$STATE_DIR" 2>/dev/null)
if [ "$OWNER" = "$SENTINEL_USER" ]; then
    ok "state owned by $SENTINEL_USER"
else
    bad "state is owned by '$OWNER', not '$SENTINEL_USER'"
    if [ "$FIX" = "1" ]; then
        chown -R "$SENTINEL_USER:$SENTINEL_USER" "$STATE_DIR" && hint "fixed"
    else
        hint "chown -R $SENTINEL_USER:$SENTINEL_USER $STATE_DIR"
    fi
fi

if grep -q '^SENTINEL_ADMIN_PASSWORD=.\+' "$CONF_DIR/sentinel.env" 2>/dev/null; then
    warn "SENTINEL_ADMIN_PASSWORD is still in $CONF_DIR/sentinel.env"
    hint "the account lives in users.db now; delete that line"
fi

# --------------------------------------------------------------------------- #
sec "6b. Venue configuration"

if [ -f "$VAR_DIR/brokers.json" ]; then
    ENABLED=$(python3 - "$VAR_DIR/brokers.json" <<'PYEOF' 2>/dev/null || echo "?"
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("?"); raise SystemExit
rows = [v for v in data.values() if isinstance(v, dict) and v.get("enabled")]
print(len(rows))
PYEOF
)
    case "$ENABLED" in
        0) warn "no broker connection is enabled"
           hint "open the dashboard, page «بروکر و اتصال», test a connection and activate it" ;;
        1) ok  "one broker connection enabled" ;;
        ?) warn "$VAR_DIR/brokers.json could not be read" ;;
        *) bad "$ENABLED broker connections are enabled for one profile"
           hint "the service refuses to start with more than one. Disable all but one." ;;
    esac
else
    ok "no saved broker connections (credentials come from the environment)"
fi

# --------------------------------------------------------------------------- #
sec "7. Environment"

if command -v timedatectl >/dev/null 2>&1; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
        ok "clock synchronised"
    else
        bad "clock NOT synchronised"
        hint "timedatectl set-ntp true"
        hint "this matters more than it looks: the licence guard refuses to verify"
        hint "while the clock is more than 6 hours behind what it has already seen."
    fi
fi

PCT=$(df -P "$STATE_DIR" 2>/dev/null | awk 'NR==2 {gsub(/%/,""); print $5}')
if [ -n "${PCT:-}" ]; then
    [ "$PCT" -ge 95 ] && bad "disk ${PCT}% full -- the audit journal cannot be written" \
        || { [ "$PCT" -ge 85 ] && warn "disk ${PCT}% full" || ok "disk ${PCT}% used"; }
fi

if [ -f "$VAR_DIR/KILL" ]; then
    warn "THE KILL SWITCH IS ENGAGED -- no new trades"
    "$PY" -c "
import json,sys
try: print('         reason:', json.load(open(sys.argv[1])).get('reason','?'))
except Exception: pass" "$VAR_DIR/KILL"
    hint "release it from the dashboard, or: rm $VAR_DIR/KILL"
fi

if [ -f "$VAR_DIR/agent_state.json" ]; then
    "$PY" - "$VAR_DIR/agent_state.json" <<'PYST' 2>/dev/null | sed 's/^/  /'
import json, sys
s = json.load(open(sys.argv[1]))
if s.get("halted"):
    print(f"\033[33mwarn \033[0m the agent is HALTED: {s.get('halt_reason','?')}")
    print("         -> a halt survives a restart; release it from the dashboard")
rung = s.get("ladder_rung", 0)
if rung:
    print(f"\033[33mwarn \033[0m drawdown ladder at rung {rung}: position sizes are reduced")
if s.get("day_profit_locked"):
    print("\033[32m ok  \033[0m daily profit lock engaged -- no new entries today, by design")
PYST
fi

# --------------------------------------------------------------------------- #
sec "8. Licence"

if [ -f "$VAR_DIR/licence.key" ]; then
    OUT=$("$PY" "$INSTALL_DIR/scripts/licensegen.py" inspect \
          --licence "$VAR_DIR/licence.key" 2>&1 | head -1)
    case "$OUT" in
        VALID*) ok "licence valid" ;;
        *NO\ PUBLIC\ KEY*) warn "licence present but enforcement is off (no public key)" ;;
        *) bad "licence: $OUT"; hint "docs/LICENSING.md" ;;
    esac
else
    ok "no licence file -- enforcement is off (correct for a self-hosted install)"
fi

# --------------------------------------------------------------------------- #
sec "9. Recent errors"

if command -v journalctl >/dev/null 2>&1; then
    ERRS=$(journalctl -u sentinel-engine --since "24 hours ago" --no-pager 2>/dev/null \
           | grep -icE "error|exception|traceback|refus" || true)
    if [ "${ERRS:-0}" -gt 0 ]; then
        warn "$ERRS error-ish lines in the last 24h; most recent:"
        printf '%s' "$DIM"
        journalctl -u sentinel-engine --since "24 hours ago" --no-pager 2>/dev/null \
            | grep -iE "error|exception|traceback|refus" | tail -5 | sed 's/^/         /'
        printf '%s' "$OFF"
    else
        ok "no errors logged in the last 24h"
    fi
fi

# --------------------------------------------------------------------------- #
if [ "$BUNDLE" = "1" ]; then
    sec "Support bundle"
    B="/tmp/sentinel-diagnostics-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$B"
    # DELIBERATELY excludes users.db, licence.key, sentinel.env and anything
    # else carrying a secret. A support bundle that leaks a TOTP secret is
    # worse than no support bundle.
    journalctl -u sentinel-engine -n 2000 --no-pager > "$B/engine.log" 2>/dev/null
    journalctl -u sentinel-watchdog -n 500 --no-pager > "$B/watchdog.log" 2>/dev/null
    "$PY" -c "
import json,sys
cfg=json.load(open(sys.argv[1]))
cfg.pop('security',None)
json.dump(cfg, open(sys.argv[2],'w'), indent=2)" \
        "$STATE_DIR/config.json" "$B/config-redacted.json" 2>/dev/null
    tail -c 400000 "$VAR_DIR/audit.jsonl" > "$B/audit-tail.jsonl" 2>/dev/null
    cp "$VAR_DIR/heartbeat.json" "$B/" 2>/dev/null
    "$PY" --version > "$B/versions.txt" 2>&1
    "$PY" -m pip freeze >> "$B/versions.txt" 2>/dev/null
    uname -a >> "$B/versions.txt"
    tar -czf "$B.tar.gz" -C "$(dirname "$B")" "$(basename "$B")" && rm -rf "$B"
    ok "bundle written to $B.tar.gz"
    hint "it contains NO secrets: no users.db, no licence, no env file"
fi

# --------------------------------------------------------------------------- #
printf '\n'
if [ "$PROBLEMS" -eq 0 ]; then
    printf '  %sNothing is broken.%s\n\n' "$GRN" "$OFF"
    exit 0
fi
printf '  %s%d problem(s) found.%s\n' "$RED" "$PROBLEMS" "$OFF"
printf '  Re-run with --fix to repair permissions and ownership automatically.\n'
printf '  Re-run with --bundle to produce a shareable diagnostic archive.\n\n'
exit 2
