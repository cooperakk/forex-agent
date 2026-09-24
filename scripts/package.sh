#!/usr/bin/env bash
# Build a distributable tarball.
#
# Includes the built dashboard so the recipient can run the system without
# Node. Excludes every piece of state: var/ holds the audit chain, the verdict
# registry and the agent's memory, and shipping someone else's acceptance
# history would be actively harmful.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="sentinel-fx"
VERSION="$(grep -m1 '^version' "$ROOT/pyproject.toml" | cut -d'"' -f2)"
OUT_DIR="${1:-$ROOT/dist}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cd "$ROOT"

if [ ! -f dashboard/dist/index.html ]; then
    echo "[package] building the dashboard first"
    (cd dashboard && npm run build)
fi

DEST="$STAGE/$NAME"
mkdir -p "$DEST"

# Source, docs, deployment, tests. Note dashboard/src is included: the
# recipient should be able to read and rebuild the UI, not just run it.
for path in sentinel scripts tests docs deploy dashboard/src dashboard/dist; do
    mkdir -p "$DEST/$(dirname "$path")"
    cp -R "$path" "$DEST/$path"
done

for f in README.md Makefile pyproject.toml pytest.ini requirements.txt \
         requirements-dev.txt Dockerfile docker-compose.yml .dockerignore \
         .gitignore .env.example \
         dashboard/package.json dashboard/package-lock.json \
         dashboard/index.html dashboard/vite.config.ts dashboard/tsconfig.json \
         dashboard/build-artifact.mjs; do
    [ -e "$f" ] && { mkdir -p "$DEST/$(dirname "$f")"; cp "$f" "$DEST/$f"; }
done

mkdir -p "$DEST/var" && touch "$DEST/var/.gitkeep"

# The guide a recipient opens first: the Persian install/diagnose guide at the
# top level, as Markdown and -- when the renderer is available -- as a
# self-contained right-to-left HTML page that opens with a double-click.
PY="${PYTHON:-python3}"
cp docs/INSTALL-FA.md "$DEST/START-HERE-FA.md"
if "$PY" -c "import markdown" 2>/dev/null; then
    "$PY" scripts/render_guide.py docs/INSTALL-FA.md "$DEST/START-HERE-FA.html"
    "$PY" scripts/render_guide.py docs/RAHNAMA-FA.md "$DEST/docs/RAHNAMA-FA.html"
else
    echo "[package] note: 'markdown' is not installed; the HTML guide is skipped"
fi

find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -name '*.pyc' -delete 2>/dev/null || true
find "$DEST" -name '.DS_Store' -delete 2>/dev/null || true
rm -f "$DEST/dashboard/tsconfig.tsbuildinfo"

# Fail loudly rather than shipping a secret or someone else's state.
# private.pem is the one that would end the business: it is the licence
# signing key, and shipping it makes every licence ever issued forgeable.
# broker-secrets.key is the second worst: it opens every broker password the
# customer saved. brokers.json holds no password but does hold account numbers
# and server names, which is an inventory of somebody's accounts.
LEAKS=$(find "$DEST" \( -name '.env' -o -name '*.db' -o -name 'audit.jsonl' \
                        -o -name 'agent_state.json' -o -name 'private.pem' \
                        -o -name 'licence.key' -o -name 'vendor-keys' \
                        -o -name 'enrolment-*.txt' \
                        -o -name 'broker-secrets.json' \
                        -o -name 'broker-secrets.key' \
                        -o -name '*.key' \
                        -o -name 'brokers.json' \
                        -o -name 'licence-timing.json' \) -print)
if [ -n "$LEAKS" ]; then
    echo "[package] REFUSING: state or secrets found in the staged tree:" >&2
    echo "$LEAKS" >&2
    exit 1
fi

# A checksum for every file, so a recipient can tell a damaged or altered
# download from the real one (sha256sum -c SHA256SUMS.txt, or Get-FileHash).
if command -v sha256sum >/dev/null; then
    (cd "$DEST" && find . -type f ! -name SHA256SUMS.txt -print0 | sort -z \
        | xargs -0 sha256sum > SHA256SUMS.txt)
fi

mkdir -p "$OUT_DIR"
TARBALL="$OUT_DIR/$NAME-$VERSION.tar.gz"
tar --numeric-owner --owner=0 --group=0 -czf "$TARBALL" -C "$STAGE" "$NAME"
echo "[package] $TARBALL"
echo "[package] $(du -h "$TARBALL" | cut -f1)  $(tar -tzf "$TARBALL" | wc -l) entries"

# Windows gets a .zip: Explorer opens it natively and Update.ps1 accepts it.
# PowerShell and .cmd files keep their CRLF line endings from the checkout.
ZIPFILE="$OUT_DIR/$NAME-$VERSION.zip"
rm -f "$ZIPFILE"
if command -v zip >/dev/null; then
    (cd "$STAGE" && zip -qr -X "$ZIPFILE" "$NAME")
else
    "$PY" - "$STAGE" "$NAME" "$ZIPFILE" <<'PYZIP'
import os, sys, zipfile
stage, name, out = sys.argv[1:4]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _dirs, files in os.walk(os.path.join(stage, name)):
        for f in sorted(files):
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, stage))
PYZIP
fi
echo "[package] $ZIPFILE  $(du -h "$ZIPFILE" | cut -f1)"
command -v sha256sum >/dev/null && (cd "$OUT_DIR" && sha256sum "$(basename "$TARBALL")" \
    "$(basename "$ZIPFILE")" | tee "$NAME-$VERSION.sha256")
