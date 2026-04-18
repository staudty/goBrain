#!/usr/bin/env bash
# Mac Mini: install llama.cpp, download the Qwen 3.5 35B-A3B MoE (UD-IQ3_XXS),
# install a LaunchAgent for on-demand heavy-tier inference on port 8081.
# Idempotent. Safe to re-run.
set -euo pipefail

log() { printf "\033[1;35m[setup-llamacpp]\033[0m %s\n" "$*"; }

MODEL_DIR="$HOME/.local/share/llama-models"
MODEL_FILE="Qwen3.5-35B-A3B-UD-IQ3_XXS.gguf"
MODEL_PATH="$MODEL_DIR/$MODEL_FILE"
HF_REPO="unsloth/Qwen3.5-35B-A3B-GGUF"

# ---- 1. Install llama.cpp via Homebrew --------------------------------------
if ! command -v llama-server >/dev/null 2>&1; then
  log "Installing llama.cpp via Homebrew"
  brew install llama.cpp
else
  log "llama.cpp already installed: $(llama-server --version 2>&1 | head -1)"
fi

# ---- 2. Install huggingface-hub for downloads -------------------------------
if ! python3 -c "import huggingface_hub" 2>/dev/null; then
  log "Installing huggingface_hub"
  pip3 install --user --break-system-packages huggingface-hub
fi

# ---- 3. Download model ------------------------------------------------------
mkdir -p "$MODEL_DIR"
if [ -f "$MODEL_PATH" ]; then
  log "Model already downloaded: $MODEL_PATH ($(du -h "$MODEL_PATH" | awk '{print $1}'))"
else
  log "Downloading $MODEL_FILE from Hugging Face (~13 GB, takes a while)"
  python3 - <<PY
from huggingface_hub import hf_hub_download
import os
out = hf_hub_download(
    repo_id="$HF_REPO",
    filename="$MODEL_FILE",
    local_dir="$MODEL_DIR",
)
print("Downloaded to", out)
PY
fi

# ---- 4. Install LaunchAgent -------------------------------------------------
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/launchd/com.gobag.llamacpp.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.gobag.llamacpp.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__MODEL_PATH__|$MODEL_PATH|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# Note: we do NOT load it by default. Use `launchctl load` to start it on demand.
log "LaunchAgent written to $PLIST_DST"
log "Start heavy tier:  launchctl load   $PLIST_DST"
log "Stop heavy tier:   launchctl unload $PLIST_DST"

# ---- 5. Optional one-shot smoke test ----------------------------------------
if [ "${1:-}" = "--smoke-test" ]; then
  log "Starting llama-server for smoke test (ctrl-c to stop after check)"
  llama-server \
    --model "$MODEL_PATH" \
    --port 8081 --ctx-size 4096 --n-gpu-layers 0 --mmap \
    --flash-attn on --threads 8 &
  LS_PID=$!
  sleep 15
  log "Querying heavy tier"
  time curl -s http://localhost:8081/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen","messages":[{"role":"user","content":"Reply with: OK"}],"max_tokens":10}' \
    | grep -oE '"content":"[^"]+"' | head -1
  kill "$LS_PID" 2>/dev/null || true
fi

log "Done. llama.cpp is ready; model at $MODEL_PATH."
