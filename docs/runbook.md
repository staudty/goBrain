# Runbook

Day-to-day operations and the full deploy sequence. Replace the placeholders
(`<NAS-IP>`, `<vault>`, `<user>`, `<mac-mini>`, etc.) with values from your
environment.

## First-time deploy (in order)

### 1. Storage host — Postgres + pgvector

```bash
ssh <user>@<NAS-IP>
sudo mkdir -p /volume1/docker/brain-db
sudo chown "$USER":users /volume1/docker/brain-db
cd /volume1/docker/brain-db

# Copy compose and init.sql from the repo onto the storage host
# (e.g. scp, rsync, or clone the repo there and use compose/postgres/)

cp .env.example .env
# Edit .env — set POSTGRES_PASSWORD (openssl rand -base64 32)
nano .env

docker compose up -d
docker compose logs -f postgres   # watch for "database system is ready"

# Sanity check
docker compose exec postgres psql -U brain -d brain -c "\dx"
# Should show: vector, pg_trgm, uuid-ossp
```

### 2. Always-on host — Ollama

```bash
cd ~/goBrain/mac-mini
./setup-ollama.sh
```

Verifies: Ollama installed, models pulled, LaunchAgent loaded, smoke test passes.

The LaunchAgent binds Ollama to `0.0.0.0:11434` by default so cross-host MCP
clients can reach it. If that's not what you want, edit
`mac-mini/launchd/com.gobag.ollama.plist` and set `OLLAMA_HOST=127.0.0.1:11434`
before running the setup script.

### 3. Always-on host — llama.cpp (optional, heavy tier)

```bash
cd ~/goBrain/mac-mini
./setup-llamacpp.sh              # installs + downloads ~13 GB model
# Start heavy tier:
launchctl load ~/Library/LaunchAgents/com.gobag.llamacpp.plist
# Stop heavy tier:
launchctl unload ~/Library/LaunchAgents/com.gobag.llamacpp.plist
```

### 4. Always-on host — ingester

```bash
cd ~/goBrain/ingester
cp .env.example .env              # edit vault path + Postgres DSN
uv sync
uv run brain-ingester             # foreground, watch logs

# When happy, install as a LaunchAgent / systemd unit.
# macOS: cd ../mac-mini && ./setup-ingester.sh
```

### 5. Each client machine — MCP server

```bash
cd ~/goBrain/mcp-server
cp .env.example .env
# Edit — Postgres DSN, Ollama base URL (always-on host's LAN IP), local vault path
uv sync

# Register with Claude Code (stdio transport)
claude mcp add brain --scope user -- uv run --directory $(pwd) brain-mcp

# Register with Claude Desktop — edit its config, e.g. on macOS:
#   ~/Library/Application Support/Claude/claude_desktop_config.json
# {
#   "mcpServers": {
#     "brain": {
#       "command": "uv",
#       "args": ["run", "--directory", "<absolute path to mcp-server>", "brain-mcp"]
#     }
#   }
# }
```

### 6. Optional — Windows client Claude Code shipper

```powershell
cd ~\goBrain\windows
.\install-shipper.ps1
```

Registers a scheduled task that runs every 10 minutes, finds Claude Code
JSONLs older than 5 minutes, and copies them to your sync folder so the
always-on host's multi-dir watcher can ingest them.

## Health checks

```bash
# Ingester
curl http://<ingester-host>:8765/health

# Ollama
curl http://<ollama-host>:11434/api/version

# llama.cpp (only if heavy tier is loaded)
curl http://<llamacpp-host>:8081/v1/models

# Postgres
psql "postgresql://brain:YOUR_PASSWORD@<NAS-IP>:5433/brain" -c "SELECT count(*) FROM documents;"
```

## Dashboard

The ingester ships a zero-build dashboard at:

```
http://<ingester-host>:8765/dashboard
```

Shows total docs, per-source counts, ingest velocity, recent sessions, and
SQLite buffer depth. Polls the ingester's `/stats/*` JSON endpoints every
few seconds.

## Common operations

### Manually ingest a file

```bash
cp /path/to/grok_export.json <vault>/_inbox/
# The inbox watcher picks it up within a few seconds; check logs.
```

### Backfill all historical Claude Code sessions

```bash
# Re-ingests every JSONL the ingester can see (primary + extra directories).
# Dedup on (source, source_id, raw_hash) means already-ingested sessions
# skip fast. Essential after a Postgres wipe, or to pull in sessions that
# existed before the ingester was installed.
curl -X POST http://<ingester-host>:8765/admin/reingest/claude-code
# Add ?background=false to block until done.
```

### Re-process everything in the inbox

```bash
curl -X POST http://<ingester-host>:8765/admin/reingest/inbox
```

### Drain SQLite buffer into Postgres

```bash
# Used after Postgres downtime to flush buffered writes.
curl -X POST http://<ingester-host>:8765/admin/drain-buffer
```

### Back up Postgres

Take regular filesystem snapshots of the Postgres data volume. In addition,
run a weekly `pg_dump`:

```bash
# On the storage host, via a scheduled task
docker compose exec -T postgres pg_dump -U brain -d brain --format=c \
  > /volume1/backups/brain-db/brain-$(date +%Y%m%d).dump
```

### Inspect the HNSW index

```sql
SELECT schemaname, tablename, indexname, pg_size_pretty(pg_relation_size(indexrelid))
FROM pg_stat_user_indexes s
JOIN pg_class c ON c.oid = s.indexrelid
WHERE indexname = 'chunks_embedding_hnsw_idx';
```

## Failure modes & recovery

| Symptom | Fix |
|---|---|
| Ingester 500s with "Postgres not configured" | Set `BRAIN_POSTGRES_DSN` in ingester `.env` and restart |
| `search_brain` returns nothing, but docs exist | Check Ollama is up on port 11434; embed model present (`ollama list`) |
| `search_brain` hangs during bulk ingest | Expected under contention. MCP auto-falls back to raw ANN top-K when a non-rerank model is loaded. Verify retrieval.py has `_rerank_feasible()` |
| `search_brain` returns stale / missing content after a Postgres wipe | `POST /admin/reingest/claude-code`; drop exports back into `_inbox/` for Claude.ai/Grok content |
| Ingester hits `ReadTimeout` or `RemoteProtocolError` mid-batch | Fixed in current build: Ollama client timeout is 600s, and `inbox.py` catches per-conversation failures without aborting the whole batch. Verify the file isn't a 0 B on-demand sync placeholder (`du -h` it) |
| llama.cpp OOM or swap | Context too large; reduce `--ctx-size` or use a smaller quant (`UD-IQ2_M`) |
| Ingester runaway CPU | Thinking mode accidentally enabled; verify `think: false` in Ollama calls |
| Duplicates on re-ingest | Normal if `raw_hash` changed (content modified). Check `ingestion_log` for the "updated" row |
| Postgres host unreachable | Ingester auto-buffers to SQLite. Verify via `GET /health`. Drain with `/admin/drain-buffer` when restored |
| Synology Drive conflict | Resolve in DSM conflict UI; vault is markdown, safe to hand-merge |
| Cross-machine JSONLs not landing | In Drive Client → Selective Sync Settings → Sync Mode → check "Sync files and folders with the prefix '.'" (the shipper writes to `.claude-code-sources/`) |
| Files show 0 bytes on `du -h` | Drive Client "Enable On-demand Sync" is on. Either (a) recreate the Sync Task with it off, or (b) `dd if=<file> of=/dev/null bs=1M` to force materialization |
| Windows scheduled task shows a flashing PowerShell window | Re-run `install-shipper.ps1` — current version uses `wscript.exe ship-hidden.vbs` |

## Scaling notes

Keep an eye on these as the corpus grows:

- `chunks` row count > 1M: raise HNSW `ef_search` at query time for recall (`SET LOCAL hnsw.ef_search = 100;`).
- Disk usage: each conversation ≈ a few KB of text + a few KB of vectors. 10k conversations ≈ 500 MB.
- Ollama summarization queue: if ingestion bursts, the service serializes calls. Fine, just patient. For huge backfills consider temporarily raising `OLLAMA_MAX_LOADED_MODELS=2` to parallelize embed + summarize.
