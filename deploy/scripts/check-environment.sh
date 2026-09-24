#!/usr/bin/env bash
#
# Sentinel-FX -- can this Linux machine run the engine?  (run BEFORE install)
#
#   ./deploy/scripts/check-environment.sh            # no root needed
#   ./deploy/scripts/check-environment.sh --quick    # skip the network probes
#
# Read-only: it installs nothing, changes nothing, and sends nothing but TLS
# handshakes to the public endpoints the engine will need. Exit code 0 means
# "ready", 1 means "works with warnings", 2 means "will not work as is".
set -uo pipefail

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1
PORT="${PORT:-8088}"

RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; DIM=$'\033[2m'; OFF=$'\033[0m'
FAILS=0; WARNS=0
sec()  { printf '\n%s== %s ==%s\n' "$DIM" "$1" "$OFF"; }
ok()   { printf '  %s ok  %s %s\n' "$GRN" "$OFF" "$1"; }
warn() { printf '  %swarn %s %s\n' "$YEL" "$OFF" "$1"; WARNS=$((WARNS+1)); }
bad()  { printf '  %sFAIL %s %s\n' "$RED" "$OFF" "$1"; FAILS=$((FAILS+1)); }
hint() { printf '         %s-> %s%s\n' "$DIM" "$1" "$OFF"; }

# --------------------------------------------------------------------------- #
sec "System"
if [ -r /etc/os-release ]; then
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
        *debian*|*ubuntu*) ok "OS: ${PRETTY_NAME:-$ID}" ;;
        *) warn "OS: ${PRETTY_NAME:-unknown} -- the installer targets Debian/Ubuntu"
           hint "Ubuntu 24.04 LTS is the tested platform" ;;
    esac
else
    bad "cannot identify the OS (/etc/os-release missing)"
fi

arch="$(uname -m)"
case "$arch" in
    x86_64|aarch64) ok "architecture: $arch" ;;
    *) warn "architecture: $arch -- numpy/scipy may have to compile from source" ;;
esac

mem_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
if   [ "$mem_mb" -ge 1800 ]; then ok "memory: ${mem_mb} MB"
elif [ "$mem_mb" -ge 900 ];  then warn "memory: ${mem_mb} MB -- works, but research runs may be killed"
else bad "memory: ${mem_mb} MB -- at least 1 GB is required"; fi

disk_mb=$(df -Pm "${INSTALL_PARENT:-/opt}" 2>/dev/null | awk 'NR==2 {print $4}')
disk_mb=${disk_mb:-0}
if [ "$disk_mb" -ge 2000 ]; then ok "free disk on /opt: ${disk_mb} MB"
else bad "free disk on /opt: ${disk_mb} MB -- at least 2 GB is required"; fi

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    ok "systemd is the init system"
else
    bad "systemd is not running -- the installer registers systemd services"
    hint "use Docker instead:  docker compose up -d"
fi

# --------------------------------------------------------------------------- #
sec "Clock"
if command -v timedatectl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
        ok "clock synchronised with NTP"
    else
        bad "the clock is NOT synchronised -- decisions, idempotency keys and session windows depend on it"
        hint "sudo timedatectl set-ntp true"
    fi
    tz="$(timedatectl show -p Timezone --value 2>/dev/null || echo '?')"
    ok "timezone: $tz (the service runs in UTC regardless)"
else
    warn "cannot confirm clock synchronisation here (no timedatectl/systemd)"
    hint "make sure the host runs an NTP client (chrony or systemd-timesyncd)"
fi

# --------------------------------------------------------------------------- #
sec "Software"
pyfound=""
for cand in python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1 && \
       "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        pyfound="$cand"; break
    fi
done
if [ -n "$pyfound" ]; then
    ok "python: $("$pyfound" --version 2>&1) ($pyfound)"
else
    have="$(python3 --version 2>&1 || echo none)"
    warn "python 3.11+ not found (have: $have) -- the installer will try to add python3.11"
fi

if command -v node >/dev/null 2>&1; then
    major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    if [ "$major" -ge 18 ]; then ok "node: $(node --version)"
    else warn "node $(node --version) is older than 18 -- the dashboard build needs 18+"; fi
else
    warn "node not installed -- the installer will try the distribution's nodejs"
fi

for tool in curl tar sqlite3; do
    command -v "$tool" >/dev/null 2>&1 && ok "$tool present" \
        || warn "$tool missing (the installer installs it)"
done

# --------------------------------------------------------------------------- #
sec "Ports"
if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}\$"; then
        bad "port ${PORT} is already in use"
        hint "stop the other service, or set security.bind_port in the config"
    else
        ok "port ${PORT} is free"
    fi
fi

# --------------------------------------------------------------------------- #
if [ "$QUICK" -eq 0 ]; then
    sec "Outbound HTTPS (what the engine will need)"
    probe() {  # name url required(1|0)
        local code
        # curl prints 000 itself when no HTTP answer arrives; do not append a
        # fallback, or "000" becomes "000000" and reads as reachable.
        code="$(curl -sS -o /dev/null -m 8 -w '%{http_code}' "$2" 2>/dev/null)"
        code="${code:-000}"
        if [ "$code" != "000" ]; then
            ok "$1 reachable ($code)"
        elif [ "$3" = "1" ]; then
            bad "$1 NOT reachable ($2)"
        else
            warn "$1 not reachable -- only matters if you use it"
        fi
    }
    probe "PyPI (packages)"                 https://pypi.org/simple/pip/ 1
    probe "Python wheels"                   https://files.pythonhosted.org/ 1
    probe "npm registry (dashboard build)"  https://registry.npmjs.org/ 0
    probe "economic calendar"               https://nfs.faireconomy.media/ff_calendar_thisweek.json 0
    probe "Federal Reserve feed"            https://www.federalreserve.gov/feeds/press_all.xml 0
    probe "ECB feed"                        https://www.ecb.europa.eu/rss/press.html 0
    probe "Claude API"                      https://api.anthropic.com/ 0
    probe "OpenAI API"                      https://api.openai.com/ 0
    probe "Gemini API"                      https://generativelanguage.googleapis.com/ 0
    probe "DeepSeek API"                    https://api.deepseek.com/ 0
    probe "Kimi (Moonshot) API"             https://api.moonshot.ai/ 0
    probe "Jev (TypeSafe) API"              https://api.typesafe.ai/ 0
    probe "TradingView data (reference)"    https://data.tradingview.com/ 0
    probe "TradingView scanner (ratings)"   https://scanner.tradingview.com/ 0
    probe "OANDA practice API"              https://api-fxpractice.oanda.com/ 0
    hint "an unreachable news, AI or TradingView endpoint only disables that feature; trading is unaffected"
    hint "full TradingView test (websocket + ratings): .venv/bin/python scripts/tv_history.py --selftest"
fi

# --------------------------------------------------------------------------- #
sec "Summary"
if [ "$FAILS" -gt 0 ]; then
    printf '  %s%d problem(s) must be fixed before installing.%s\n' "$RED" "$FAILS" "$OFF"
    exit 2
elif [ "$WARNS" -gt 0 ]; then
    printf '  %sReady, with %d warning(s).%s  Next: sudo ./deploy/scripts/install.sh\n' \
        "$YEL" "$WARNS" "$OFF"
    exit 1
fi
printf '  %sReady.%s  Next: sudo ./deploy/scripts/install.sh\n' "$GRN" "$OFF"
exit 0
