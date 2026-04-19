# Architecture

## Goals

1. **Unify AI context.** Claude Code, Claude Desktop, Claude.ai web, Pluto (Telegram), and Grok should all be able to draw on the same memory.
2. **Capture Pluto's actions, not just its words.** Every tool call and outcome logged.
3. **Respect Claude's token budget.** A small local LLM (Gemma 4) does the librarian work; Claude only sees tight, curated context.
4. **Local-first and private.** No vault content leaves Chris's network without an explicit call to Claude/Grok.
5. **Minimally manual.** Ingestion is automatic for everything we control; for the rest (Grok, Claude.ai) a single "drop file in `_inbox/`" is the entire user step.

## Data model

### The vault (markdown)

The human-facing source of truth. Lives at `/volume1/homes/cstaudt/Brain/` on the NAS, synced to PC and Mac via Synology Drive.

```
Brain/
├── _inbox/                 # drop zone for manual exports (Grok, Claude.ai, PDFs, etc.)
│   └── _processed/         # ingester moves files here after successful ingestion
├── sessions/               # one note per ingested conversation
│   ├── claude-code/
│   │   └── YYYY-MM-DD_<project>_<short-id>.md
│   ├── claude-desktop/
│   ├── claude-ai/
│   ├── grok/
│   └── pluto/
├── pluto-activity/         # daily rollup of Pluto's tool calls and outcomes
│   └── YYYY-MM-DD.md
├── notes/                  # Chris's own notes, written directly in Obsidian
└── templates/              # Obsidian templates for consistent formatting
```

Each session note starts with YAML frontmatter used by both humans and the ingester:

```yaml
---
source: claude-code      # claude-code | claude-desktop | claude-ai | grok | pluto | telegram | inbox
source_id: <stable id>   # for dedup on re-ingestion
project: PuckEngine      # optional context tag
started_at: 2026-04-18T14:03:12-04:00
ended_at: 2026-04-18T15:41:08-04:00
turn_count: 47
tool_calls: 12
model: claude-opus-4-7
tags: [postgres, migrations]
summary: >
  2-sentence Gemma-written TL;DR.
---
```

Body is the conversation, cleaned and condensed to matter. Tool calls rendered in fenced blocks. Long outputs truncated with `<!-- truncated: N lines -->` markers.

### The database (Postgres + pgvector)

The machine-queryable index. Runs in Docker on the NAS.

```sql
-- compose/postgres/init.sql drives this; condensed view here:

documents (
  id uuid primary key,
  source text not null,              -- matches frontmatter 'source'
  source_id text not null,           -- stable id for dedup
  vault_path text not null unique,   -- 'sessions/claude-code/2026-04-18_...md'
  started_at timestamptz,
  ended_at timestamptz,
  project text,
  model text,
  turn_count int,
  tool_call_count int,
  summary text,                      -- short Gemma-written tldr
  tags text[],
  raw_hash text,                     -- content hash for change detection
  ingested_at timestamptz default now()
);

chunks (
  id uuid primary key,
  document_id uuid references documents on delete cascade,
  chunk_index int,
  content text not null,
  embedding vector(768)              -- nomic-embed-text dimension
);

```

Indexes:
- `chunks (embedding)` via HNSW for fast ANN search
- `documents (source, started_at desc)` for recency queries
- GIN index on `documents.tags`

### Nothing is thrown away

Raw JSONL logs from Claude Code, raw export ZIPs from Grok/Claude.ai, raw Telegram message dumps — all archived under `Brain/_raw/<source>/<date>/` on the NAS for disaster recovery. The vault markdown is derived; if we ever change the summarization logic, we can re-derive.

## Components

### Ollama (Mac Mini, port 11434)

Hosts the small models. One model loaded at a time (`OLLAMA_MAX_LOADED_MODELS=1`), 10-minute idle unload.

| Model | Size on disk | Active RAM | Role |
|---|---|---|---|
| `gemma4:e2b` | 7.2 GB | ~4 GB | Classification, routing, short responses |
| `gemma4:e4b` | 9.6 GB | ~6 GB | Summarization, re-ranking, context compression |
| `nomic-embed-text` | 274 MB | negligible | Embeddings for pgvector |

Thinking mode disabled by default in API calls (`"think": false`) — 30x speedup for classification, critical for triage latency.

Launched as a macOS LaunchAgent so it's always available.

### llama.cpp (Mac Mini, port 8081)

Hosts the heavy MoE model on demand via `--mmap`.

- Model: `unsloth/Qwen3.5-35B-A3B-GGUF` at `UD-IQ3_XXS` quantization (~13 GB on disk).
- MoE architecture: 35B total params, 3B active per token. With `--mmap` the OS pages experts from the NVMe on demand; only shared layers (~5 GB) stay resident.
- Observed throughput: ~8 tok/s cold, trending up with use as the macOS page cache warms around the expert-weight working set. Leopardracer's 17 tok/s was after days of continuous operation. Acceptable for heavy-tier use cases (overnight batch, episodic fallback).
- Flags: `-ngl 0 --ctx-size 16384 --reasoning off --threads 10`. (Metal init happens even at `-ngl 0`; mmap is default; `--flash-attn` defaults to auto; `--reasoning off` globally disables Qwen 3.5 thinking mode — critical for speed.)
- Use cases: nightly Pluto-activity compression, Claude fallback when rate-limited, weekly vault consolidation passes.

Run on-demand via a LaunchAgent the ingester can `launchctl kickstart` when needed; unload after idle.

### Ingester (Mac Mini, FastAPI, port 8765)

The busy bee. One Python service that:

1. **Watches local files.** `~/.claude/projects/*.jsonl` on the Mac Mini (Claude Code sessions), Claude Desktop history, the Synology-Drive-synced `_inbox/` folder on the NAS.
2. **Receives POSTs** from the Windows PC's tiny shipper and from Pluto's tool-call hook.
3. **Summarizes** each conversation by calling Ollama E4B — output: 200-word markdown summary + frontmatter + cleaned body.
4. **Embeds** each chunk (~500 tokens, 100 overlap) via nomic-embed-text.
5. **Persists** to Postgres (documents + chunks rows) and writes the markdown file to the vault.
6. **Deduplicates** by `(source, source_id)` so re-ingestion is idempotent.

Buffers to SQLite if Postgres is unreachable (handles the week before the NAS RAM arrives, or NAS downtime).

### MCP server (runs everywhere, search in one place)

An MCP stdio server installed on the Windows PC and Mac Mini, registered with Claude Code and Claude Desktop. Exposes:

- `search_brain(query: str, limit: int = 5, sources: list[str] | None = None) -> list[Chunk]`
  Gemma re-ranks the top-20 pgvector hits to top-N; returns tight chunks with source metadata, never whole documents.
- `recent_sessions(n: int = 10, source: str | None = None) -> list[DocumentSummary]`
  Chronological recent-first.
- `pluto_activity(since: str | None = None, tool: str | None = None) -> list[PlutoEvent]`
  What did Pluto do, filterable.
- `get_document(vault_path: str) -> str`
  Last-resort full-content retrieval, explicitly requested by the caller.

A **remote MCP** at `brain.gobag.dev` (Caddy → FastAPI on Mac Mini, bearer-token auth) exposes the same tools to Pluto and any cloud client.

### Pluto activity logger

A thin hook inside OpenClaw that POSTs every tool invocation to the ingester:

```json
{
  "ts": "2026-04-18T14:03:12-04:00",
  "kind": "tool_call",
  "tool_name": "send_email",
  "payload": {"to": "...", "subject": "...", "body": "..."},
  "parent_session_id": "..."
}
```

Nightly batch job on the Mac Mini reads that day's events, runs them through the 35B model for a dense rollup, writes `pluto-activity/YYYY-MM-DD.md`, and indexes it like any other document.

## Query flow (example)

From Claude Code on Windows: *"what did we decide about the Postgres migration?"*

1. Claude Code calls `search_brain("postgres migration decision")` via the local MCP server.
2. MCP server forwards to Mac Mini (or its local pgvector connection).
3. nomic-embed-text embeds the query.
4. pgvector returns top-20 chunks by cosine distance.
5. Gemma 4 E2B re-ranks to top-5 by relevance.
6. MCP returns those 5 chunks (~2.5K tokens) with metadata.
7. Claude Code answers using that context + its own reasoning.

Claude never sees the raw vault. Gemma does the librarian work.

## Privacy boundary

- **Stays local:** ingestion, embeddings, summarization, re-ranking.
- **Leaves local:** only the final Gemma-curated chunks that get passed to Claude / Grok when the caller explicitly decides to call them. Chris controls this per-session.
- **Never leaves local:** raw JSONL logs, Pluto tool-call payloads, vault full-text.

## Failure modes

| Failure | Behavior |
|---|---|
| NAS down | Ingester buffers to local SQLite. MCP returns "brain offline, falling back to Obsidian-local search". |
| Mac Mini down | Windows PC MCP server returns partial results from the locally-cached recent_sessions snapshot. |
| Ollama down | Ingester queues summaries; MCP returns raw top-K without re-ranking. |
| Postgres corrupted | Restore from snapshot replication; re-derive chunks from vault markdown. |
| Vault sync broken | Synology Drive's own conflict resolution; worst case: restore from snapshot. |
