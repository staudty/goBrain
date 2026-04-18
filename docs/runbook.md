# Runbook

Day-to-day operations and the full deploy sequence. All commands assume the
repo is cloned to `~/goBrain` on the Mac Mini and to `/volume1/docker/brain-db`
on the NAS (only the `compose/postgres/` subtree is needed there).

## First-time deploy (in order)

### 1. NAS — Postgres + pgvector (after RAM upgrade)

```bash
ssh cstaudt@192.168.1.178
sudo mkdir -p /volume1/docker/brain-db
sudo chown "$USER":users /volume1/docker/brain-db
cd /volume1/docker/brain-db

# Copy compose and init.sql from the repo
scp chris@<mac-mini>:~/goBrain/compose/postgres/docker-compose.yml .
scp chris@<mac-mini>:~/goBrain/compose/postgres/init.sql .
cp .env.example .env
# Edit .env — set POSTGRES_PASSWORD (openssl rand -base64 32)
nano .env

docker compose up -d
docker compose logs -f postgres   # watch for "database system is ready"

# Sanity check
docker compose exec postgres psql -U brain -d brain -c "\dx"
# Should show: vector, pg_trgm, uuid-ossp
```

### 2. Mac Mini — Ollama (always on)

```bash
cd ~/goBrain/mac-mini
./setup-ollama.sh
```

Verifies: Ollama installed, models pulled, LaunchAgent loaded, smoke test passes.

### 3. Mac Mini — llama.cpp (on demand)

```bash
cd ~/goBrain/mac-mini
./setup-llamacpp.sh              # installs + downloads ~13 GB model
# Start heavy tier:
launchctl load ~/Library/LaunchAgents/com.gobag.llamacpp.plist
# Stop heavy tier:
launchctl unload ~/Library/LaunchAgents/com.gobag.llamacpp.plist
```

### 4. Mac Mini — ingester

```bash
cd ~/goBrain/ingester
cp .env.example .env              # edit vault path, postgres DSN once NAS is up
uv sync
uv run brain-ingester             # foreground, watch logs

# When happy, install as LaunchAgent (template in mac-mini/launchd/, Phase 3)
```

### 5. Mac Mini + Windows PC — MCP server

```bash
cd ~/goBrain/mcp-server
cp .env.example .env
uv sync

# Register with Claude Code (stdio transport)
claude mcp add brain "uv run --directory $(pwd) brain-mcp" --scope user

# Register with Claude Desktop — edit ~/Library/Application Support/Claude/claude_desktop_config.json:
# {
#   "mcpServers": {
#     "brain": {
#       "command": "uv",
#       "args": ["run", "--directory", "/Users/chris/goBrain/mcp-server", "brain-mcp"]
#     }
#   }
# }
```

## Health checks

```bash
# Ingester
curl http://127.0.0.1:8765/health

# Ollama
curl http://127.0.0.1:11434/api/version

# llama.cpp (only if heavy tier is loaded)
curl http://127.0.0.1:8081/v1/models

# Postgres (from Mac Mini)
psql "postgresql://brain:YOUR_PASSWORD@192.168.1.178:5433/brain" -c "SELECT count(*) FROM documents;"
```

## Drain SQLite buffer into Postgres

After Postgres is reachable:

```bash
curl -X POST http://127.0.0.1:8765/admin/drain-buffer
```

## Common operations

### Manually ingest a file

```bash
cp /path/to/grok_export.json /Users/chris/Brain/_inbox/
# The inbox watcher picks it up within a few seconds; check logs.
```

### Re-chunk / re-embed a document (if chunking strategy changes)

TBD — will ship as `brain-ingester reindex <vault_path>` in Phase 9.

### Back up Postgres

Synology snapshot replication covers `/volume1/docker/brain-db/data` automatically.
Plus a weekly `pg_dump`:

```bash
# Run on the NAS via a scheduled task
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
| llama.cpp OOM or swap | Context too large; reduce `--ctx-size` or use a smaller quant (`UD-IQ2_M`) |
| Ingester runaway CPU | Thinking mode accidentally enabled; check `think: false` in Ollama calls |
| Duplicates on re-ingest | Normal if `raw_hash` changed (content modified). Check ingestion_log for the "updated" row |
| NAS Postgres unreachable | Ingester auto-buffers to SQLite. Verify via `GET /health`. Drain with `/admin/drain-buffer` when restored |
| Synology Drive conflict | Resolve in DSM conflict UI; vault is markdown, safe to hand-merge |

## Scaling notes

Keep an eye on these as the corpus grows:

- `chunks` row count > 1M: consider raising HNSW `ef_search` at query time for recall (SET LOCAL hnsw.ef_search = 100;).
- Disk usage on NAS: each conversation ≈ a few KB of text + a few KB of vectors. 10k conversations ≈ 500 MB. Not a concern at this scale.
- Ollama E4B summarization queue: if ingestion bursts (e.g., initial Claude.ai export backfill), the service will serialize calls. Fine, just patient. For big backfills consider temporarily raising `OLLAMA_MAX_LOADED_MODELS=2` to parallelize fast+primary.
