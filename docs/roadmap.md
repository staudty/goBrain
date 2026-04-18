# Roadmap

Today = Saturday, April 18, 2026. NAS RAM arrives Thu/Fri (Apr 23–24).

Strategy: do everything that doesn't depend on the NAS Postgres before the RAM arrives. The ingester buffers to local SQLite in the meantime. Thursday is "flip the switch," not "start building."

## Phase 0 — Prep (today, Sat Apr 18)

- [x] Order 4 GB DDR4-2666 SODIMM for NAS (Crucial CT4G4SFS8266, arriving Thu/Fri)
- [ ] Scaffold the `goBrain` repo (this commit)
- [ ] Architecture + roadmap + runbook docs
- [ ] Postgres + pgvector Docker Compose + init.sql
- [ ] Mac Mini setup scripts (Ollama + llama.cpp)
- [ ] Ingester Python package skeleton
- [ ] MCP server Python package skeleton

## Phase 1 — Obsidian + vault (Sun Apr 19)

**Chris's work, ~30 min:**

- [ ] Install Obsidian desktop on Windows PC
- [ ] Install Obsidian desktop on Mac Mini
- [ ] Create folder `/volume1/homes/cstaudt/Brain/` on NAS (or confirm preferred path)
- [ ] Configure Synology Drive Client on PC and Mac to sync `Brain/` from NAS
- [ ] Open the synced folder as an Obsidian vault on both machines
- [ ] Drop a test file from PC, confirm it appears on Mac within a minute

## Phase 2 — Mac Mini LLM stack (Mon Apr 20)

- [ ] Run `mac-mini/setup-ollama.sh` (installs Ollama, pulls models, sets env, installs LaunchAgent)
- [ ] Run `mac-mini/setup-llamacpp.sh` (installs llama.cpp, downloads Qwen 35B-A3B MoE)
- [ ] Benchmark: 10 classifications via E2B, expected ~2s each
- [ ] Benchmark: 3 summarizations via E4B, expected ~10s each at 8K context
- [ ] Benchmark: llama.cpp Qwen 35B-A3B with `--mmap`, expected ~17 tok/s (matches leopardracer's measurement)
- [ ] Verify `nomic-embed-text` via `ollama run nomic-embed-text "test"`

## Phase 3 — Ingester v1 (Tue Apr 21)

- [ ] Stand up ingester FastAPI on Mac Mini, port 8765
- [ ] Claude Code JSONL watcher (Mac Mini local files)
- [ ] Summarizer calling Ollama E4B with `think: false`
- [ ] Embedder calling Ollama nomic-embed-text
- [ ] SQLite temporary storage (will swap to Postgres Thursday)
- [ ] First real session ingestion end-to-end: pick a recent Claude Code session, verify summary + chunks + markdown output
- [ ] LaunchAgent so ingester starts on boot

## Phase 4 — Remaining ingestion sources (Wed Apr 22)

- [ ] Windows PC shipper: tiny Python script, installed as a Scheduled Task, tails `%USERPROFILE%\.claude\projects\**\*.jsonl`, POSTs new lines to Mac Mini ingester
- [ ] Claude Desktop history watcher (Mac Mini + Windows PC)
- [ ] `_inbox/` folder watcher (reads from the Synology-Drive-synced `Brain/_inbox/`)
- [ ] Grok export parser (xAI JSON format)
- [ ] Claude.ai export parser (Anthropic ZIP format)

## Phase 5 — MCP server v1 (Wed Apr 22, alongside Phase 4)

- [ ] `search_brain(query, limit, sources)` — pgvector top-20 → Gemma re-rank → top-5
- [ ] `recent_sessions(n, source)` — chronological recent-first
- [ ] `get_document(vault_path)` — explicit full-content retrieval
- [ ] Register with Claude Code on both machines
- [ ] Register with Claude Desktop on both machines
- [ ] First real query: ask Claude Code to recall something from an earlier session

## Phase 6 — Switch on Postgres (Thu/Fri Apr 23–24, RAM arrives)

**Chris's work, ~2 min:**

- [ ] Power down NAS, install RAM, power on
- [ ] Verify DSM shows 6 GB total memory
- [ ] `ssh` in, `cd /volume1/docker/brain-db`, `docker compose up -d`

**My work, ~30 min:**

- [ ] Ingester config flip: SQLite → Postgres
- [ ] Backfill: replay buffered SQLite → Postgres
- [ ] Verify `search_brain` returns hits
- [ ] Tune HNSW index parameters based on corpus size

## Phase 7 — Pluto integration (Sat Apr 25)

- [ ] Pluto activity hook: POST every tool call to ingester
- [ ] `pluto_activity(since, tool)` MCP tool
- [ ] Nightly 35B compression job writing `pluto-activity/YYYY-MM-DD.md`
- [ ] Expose ingester + MCP remotely at `brain.gobag.dev` via Caddy with bearer auth
- [ ] Register remote MCP with Pluto

## Phase 8 — Secondary sources & polish (Week 2)

- [ ] Telegram "forward to brain" channel Pluto watches
- [ ] One-time backfill: Chris's existing Grok export
- [ ] One-time backfill: Chris's existing Claude.ai export
- [ ] Evaluate Gemma 4 26B MoE as potential replacement for Qwen 35B heavy tier
- [ ] Obsidian plugin / plugin config for nice in-vault search UI
- [ ] Weekly digest: Gemma writes a "here's what we discussed" summary every Sunday

## Phase 9 — Hardening (Week 3+)

- [ ] Off-site encrypted backup of Postgres + vault via rclone → Backblaze B2
- [ ] Monitoring: ingester health ping to Pluto on failure
- [ ] Re-ingestion tooling: rebuild chunks/embeddings from vault markdown if model or chunking strategy changes
- [ ] Per-source retention policies if needed (default: keep forever)

## Out of scope for now (captured so we don't forget)

- Multimodal ingestion (Gemma 4 E2B/E4B can process images — future use: auto-tag Brightwheel photos)
- PuckEngine auto-tweet pipeline (separate project, unblocked by PuckEngine public hosting)
- Home Assistant voice integration (future)
- Family-shared vault partitions (future, if Scarlett ever has her own notes)
