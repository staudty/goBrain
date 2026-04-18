#!/usr/bin/env bash
# Register the brain MCP server with Claude Code and Claude Desktop.
# Run on Mac Mini and on Windows PC (via WSL or Git Bash).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MCP_DIR="$REPO_ROOT/mcp-server"

log() { printf "\033[1;36m[register-mcp]\033[0m %s\n" "$*"; }

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not installed. Install from https://github.com/astral-sh/uv" >&2
  exit 1
fi

log "Ensuring mcp-server deps are installed"
(cd "$MCP_DIR" && uv sync)

if command -v claude >/dev/null 2>&1; then
  log "Registering with Claude Code CLI"
  claude mcp add brain "uv run --directory $MCP_DIR brain-mcp" --scope user || true
else
  log "Claude Code CLI not found on PATH; skipping CLI registration"
fi

# Claude Desktop config (macOS path; adapt on Windows)
CFG_MAC="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
if [ "$(uname -s)" = "Darwin" ] && [ -f "$CFG_MAC" ]; then
  log "Claude Desktop config detected at $CFG_MAC"
  log "Add the following to mcpServers manually, or run `jq` to merge:"
  cat <<EOF
  "brain": {
    "command": "uv",
    "args": ["run", "--directory", "$MCP_DIR", "brain-mcp"]
  }
EOF
fi

log "Done."
