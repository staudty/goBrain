#!/usr/bin/env bash
# Install the Pluto daily-rollup LaunchAgent on the Mac Mini.
# Runs every night at 03:05 local and calls the ingester's
# /admin/pluto/rollup endpoint (which summarizes yesterday's pluto_events
# into a markdown note in the vault).
set -euo pipefail

log() { printf "\033[1;36m[setup-pluto-rollup]\033[0m %s\n" "$*"; }

PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/launchd/com.gobag.pluto-rollup.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.gobag.pluto-rollup.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"
log "LaunchAgent written to $PLIST_DST"

# Replace any existing version
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
log "LaunchAgent loaded"

# Quick health check of the endpoint itself (without actually running rollup)
if curl -sf --max-time 5 http://127.0.0.1:8765/health >/dev/null 2>&1; then
  log "Ingester is reachable on :8765"
else
  log "WARNING: ingester not reachable at http://127.0.0.1:8765; rollup will fail until it is"
fi

log "Done. Will run nightly at 03:05 local."
log "Test it now with:  curl -X POST 'http://127.0.0.1:8765/admin/pluto/rollup?target_date=YYYY-MM-DD'"
