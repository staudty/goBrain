#!/usr/bin/env bash
# Mac Mini: render and install the remote MCP server LaunchAgent.
# Assumes uv is installed and ../mcp-server/.env has BRAIN_MCP_REMOTE_BEARER_TOKEN set.
# Idempotent. Safe to re-run.
set -euo pipefail

log() { printf "\033[1;32m[setup-mcp-remote]\033[0m %s\n" "$*"; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MCP_DIR="$REPO_ROOT/mcp-server"

# ---- 1. Sanity checks -------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not installed. Install with: brew install uv" >&2
  exit 1
fi

if [ ! -f "$MCP_DIR/.env" ]; then
  echo ".env missing at $MCP_DIR/.env — copy from .env.example first." >&2
  exit 1
fi

if ! grep -qE '^BRAIN_MCP_REMOTE_BEARER_TOKEN=.+' "$MCP_DIR/.env"; then
  echo "BRAIN_MCP_REMOTE_BEARER_TOKEN is empty in $MCP_DIR/.env." >&2
  echo "Generate one with: openssl rand -hex 32" >&2
  exit 1
fi

# ---- 2. Make sure deps are synced ------------------------------------------
log "Running uv sync in $MCP_DIR"
(cd "$MCP_DIR" && uv sync)

# ---- 3. Render the plist template ------------------------------------------
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/launchd/com.gobag.mcp-remote.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.gobag.mcp-remote.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__REPO__|$REPO_ROOT|g" \
    "$PLIST_SRC" > "$PLIST_DST"

log "LaunchAgent written to $PLIST_DST"

# ---- 4. (Re)load -----------------------------------------------------------
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
log "LaunchAgent loaded: com.gobag.mcp-remote"

# ---- 5. Wait for health ----------------------------------------------------
PORT="$(grep -E '^BRAIN_MCP_HTTP_PORT=' "$MCP_DIR/.env" | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-8766}"

log "Waiting for remote MCP to come up on port $PORT"
for i in {1..30}; do
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    log "Remote MCP is up"
    curl -s "http://127.0.0.1:$PORT/health" | python3 -m json.tool
    echo ""
    log "Still to do:"
    echo "  1. Put a TLS reverse proxy in front of 127.0.0.1:$PORT (see docs/remote-mcp.md)."
    echo "  2. Register the public https URL as a custom connector in Claude iOS."
    echo "     Paste the bearer token from .env when Claude asks for auth."
    exit 0
  fi
  sleep 1
done

echo "Remote MCP did not respond within 30s. Check logs:" >&2
echo "  tail ~/Library/Logs/gobag-mcp-remote.err.log" >&2
exit 1
