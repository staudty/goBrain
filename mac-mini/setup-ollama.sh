#!/usr/bin/env bash
# Mac Mini: install Ollama, pull Gemma 4 + nomic-embed, install LaunchAgent.
# Idempotent. Safe to re-run.
set -euo pipefail

log() { printf "\033[1;34m[setup-ollama]\033[0m %s\n" "$*"; }

# ---- 1. Install Ollama via Homebrew if missing ------------------------------
if ! command -v ollama >/dev/null 2>&1; then
  log "Installing Ollama via Homebrew"
  brew install ollama
else
  log "Ollama already installed: $(ollama --version)"
fi

# Minimum version for Gemma 4 support (per leopardracer's notes).
OLLAMA_MIN_VERSION="0.20.0"
CURRENT_VERSION="$(ollama --version 2>/dev/null | awk '{print $NF}' || echo 0)"
if ! printf '%s\n%s\n' "$OLLAMA_MIN_VERSION" "$CURRENT_VERSION" | sort -V -C; then
  log "Upgrading Ollama (current: $CURRENT_VERSION, need >= $OLLAMA_MIN_VERSION)"
  brew upgrade ollama
fi

# ---- 2. Environment tuning for 16GB unified memory --------------------------
# These are exported into the LaunchAgent plist below; we also write them here
# so interactive shells pick them up.
ENV_SNIPPET_MARKER="# >>> goBrain ollama env >>>"
PROFILE="$HOME/.zprofile"
if ! grep -q "$ENV_SNIPPET_MARKER" "$PROFILE" 2>/dev/null; then
  log "Adding Ollama env vars to $PROFILE"
  cat >> "$PROFILE" <<'EOF'

# >>> goBrain ollama env >>>
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0
export OLLAMA_KEEP_ALIVE=10m
export OLLAMA_MAX_LOADED_MODELS=1
# <<< goBrain ollama env <<<
EOF
fi

# ---- 3. Start Ollama temporarily so we can pull models ----------------------
if ! pgrep -x ollama >/dev/null; then
  log "Starting Ollama in background to pull models"
  OLLAMA_FLASH_ATTENTION=1 \
  OLLAMA_KV_CACHE_TYPE=q8_0 \
  OLLAMA_MAX_LOADED_MODELS=1 \
    ollama serve >/tmp/ollama-setup.log 2>&1 &
  OLLAMA_PID=$!
  # Wait for the server to come up
  for i in {1..30}; do
    if curl -sf http://localhost:11434/api/version >/dev/null 2>&1; then break; fi
    sleep 1
  done
  STARTED_OLLAMA=1
else
  STARTED_OLLAMA=0
fi

# ---- 4. Pull models ---------------------------------------------------------
# Gemma 4 E2B: fast tier (2.3B effective, vision+audio).
# Gemma 4 E4B: primary tier (4.5B effective, summarization).
# nomic-embed-text: embeddings for pgvector.
for model in gemma4:e2b gemma4:e4b nomic-embed-text; do
  if ollama list | awk 'NR>1 {print $1}' | grep -qx "$model"; then
    log "Model $model already present"
  else
    log "Pulling $model"
    ollama pull "$model"
  fi
done

# ---- 5. Stop our temporary ollama so the LaunchAgent owns the process -------
if [ "${STARTED_OLLAMA:-0}" -eq 1 ]; then
  log "Stopping temporary Ollama"
  kill "${OLLAMA_PID:-}" 2>/dev/null || true
  wait "${OLLAMA_PID:-}" 2>/dev/null || true
fi

# ---- 6. Install LaunchAgent so Ollama is always on --------------------------
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/launchd/com.gobag.ollama.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.gobag.ollama.plist"

mkdir -p "$HOME/Library/LaunchAgents"
# Substitute $HOME in the plist
sed "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"

# Reload
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
log "LaunchAgent loaded: com.gobag.ollama"

# ---- 7. Smoke test ----------------------------------------------------------
for i in {1..30}; do
  if curl -sf http://localhost:11434/api/version >/dev/null; then break; fi
  sleep 1
done
log "Ollama up at http://localhost:11434"

log "Running a classification smoke test on gemma4:e2b (think: false)"
time curl -s http://localhost:11434/api/chat \
  -d '{"model":"gemma4:e2b","messages":[{"role":"user","content":"Reply with exactly one word: CLASS"}],"think":false,"stream":false,"options":{"num_ctx":512}}' \
  | grep -oE '"content":"[^"]+"' | head -1

log "Done. Ollama is live with gemma4:e2b, gemma4:e4b, nomic-embed-text."
