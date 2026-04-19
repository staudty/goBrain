#!/usr/bin/env bash
# Mac Mini: render and install the ingester LaunchAgent.
# Assumes uv is installed and .env is already set up in ../ingester/.
# Idempotent. Safe to re-run.
set -euo pipefail

log() { printf "\033[1;32m[setup-ingester]\033[0m %s\n" "$*"; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INGESTER_DIR="$REPO_ROOT/ingester"

# ---- 1. Sanity checks -------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not installed. Install with: brew install uv" >&2
  exit 1
fi

if [ ! -f "$INGESTER_DIR/.env" ]; then
  echo ".env missing at $INGESTER_DIR/.env — copy from .env.example first." >&2
  exit 1
fi

# ---- 2. Make sure deps are synced ------------------------------------------
log "Running uv sync in $INGESTER_DIR"
(cd "$INGESTER_DIR" && uv sync)

# ---- 3. Render the plist template ------------------------------------------
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/launchd/com.gobag.ingester.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.gobag.ingester.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__REPO__|$REPO_ROOT|g" \
    "$PLIST_SRC" > "$PLIST_DST"

log "LaunchAgent written to $PLIST_DST"

# ---- 4. (Re)load -----------------------------------------------------------
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
log "LaunchAgent loaded: com.gobag.ingester"

# ---- 5. Wait for health ----------------------------------------------------
log "Waiting for ingester to come up on port 8765"
for i in {1..30}; do
  if curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; then
    log "Ingester is up"
    curl -s http://127.0.0.1:8765/health | python3 -m json.tool
    exit 0
  fi
  sleep 1
done

echo "Ingester did not respond within 30s. Check logs:" >&2
echo "  tail ~/Library/Logs/gobag-ingester.err.log" >&2
exit 1
