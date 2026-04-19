# goBrain

A self-hosted **unified second brain** that captures every AI conversation and every meaningful action across Chris's stack, indexes it, and exposes it as a single searchable context surface to Claude Code, Claude Desktop, Pluto (Telegram bot on OpenClaw), Grok, and any other tool that speaks the Model Context Protocol (MCP).

## Why

AI conversations are siloed across Claude Code sessions, Claude Desktop, Claude.ai web, Pluto on Telegram, and Grok on iPhone. None of them share memory with each other. goBrain unifies them into one vault + one vector index, queryable by all of them through a single `search_brain` MCP tool.

## The stack

```
┌──────────────────────────┐      ┌────────────────────────────┐
│ Windows PC               │      │ Mac Mini (M4, 16 GB)       │
│ ──────────────           │      │ ────────────────           │
│ Claude Code CLI          │      │ Claude Code CLI            │
│ Claude Desktop           │      │ Claude Desktop             │
│ Obsidian                 │      │ Obsidian                   │
│ MCP brain (stdio)        │      │ MCP brain (stdio)          │
│                          │      │                            │
│ Scheduled task ──────────┼─Drive┤ Ingester (FastAPI :8765)   │
│ ships Claude Code JSONLs │ sync │   ├─ Claude Code watcher   │
│ to Brain/.claude-code-   │      │   ├─ _inbox/ watcher       │
│ sources/pc/              │      │   └─ reingest API          │
└──────────────────────────┘      │                            │
                                  │ Ollama :11434 (LAN 0.0.0.0)│
                                  │   gemma4:e2b  (rerank)     │
                                  │   gemma4:e4b  (summarize)  │
                                  │   nomic-embed (embeddings) │
                                  │                            │
                                  │ llama.cpp :8081 (on-demand)│
                                  │   qwen3.5-35b-a3b MoE      │
                                  └──────────┬─────────────────┘
                                             │ network
                                             ▼
┌────────────────────────────────────────────────────────────────┐
│ Synology DS224+ NAS (192.168.1.178)                            │
│ ────────────────────                                           │
│ /volume1/homes/cstaudt/Brain/        ← vault (markdown)        │
│   ├─ sessions/                        (claude-code, claude-ai, │
│   │                                    grok, inbox, pluto)     │
│   ├─ _inbox/                          (manual exports dropped) │
│   └─ .claude-code-sources/pc/         (Windows-shipped JSONLs) │
│                                                                │
│ /volume1/docker/brain-db/             ← Docker compose:        │
│   Postgres 16 + pgvector (port 5433)  documents + chunks +     │
│                                       pluto_events +           │
│                                       ingestion_log tables     │
└────────────────────────────────────────────────────────────────┘
```

## Data flow

**Ingestion (anything → brain):**
- **Claude Code Mac** — `~/.claude/projects/*.jsonl` → live file watcher on Mac ingester → Gemma E4B summarizes → nomic-embed chunks → Postgres + markdown in `sessions/claude-code/`
- **Claude Code Windows** — scheduled PowerShell task (every 10 min, silent via VBS wrapper) copies finished JSONLs (idle ≥5 min) to `Brain/.claude-code-sources/pc/` → Drive Client syncs → Mac's second Claude Code watcher picks them up
- **Claude.ai / Grok exports** — user requests data export from the vendor, drops the ZIP into `Brain/_inbox/` → inbox watcher detects format via ZIP contents (`conversations.json` = Claude.ai, `prod-grok-backend.json` = Grok) → parser yields individual conversations → same Gemma pipeline
- **Ad-hoc documents** — drop any `.md`, `.txt`, or raw backend `.json` into `_inbox/` — same pipeline

**Retrieval (any client → brain):**
- Claude Code (Mac or Windows) invokes `search_brain(query, limit, sources)` via its local MCP server
- MCP server embeds the query with nomic-embed, pulls top-K from pgvector, re-ranks with Gemma E2B, returns chunks
- If Ollama is busy with ingestion (another model pinned), rerank is skipped gracefully — raw ANN top-K returned in seconds instead of hanging on model-swap thrashing

## Hardware + software

| Role | Host | Key software |
|---|---|---|
| Postgres + pgvector | Synology DS224+ NAS | Container Manager (Docker + Compose) |
| Ingester, Ollama, llama.cpp, MCP brain | Mac Mini M4 16 GB | Homebrew (ollama, llama.cpp, uv), LaunchAgents |
| Claude Code shipper, Obsidian, MCP brain | Windows PC (RTX 5090) | PowerShell Scheduled Task + VBS launcher, uv, Claude Code CLI |
| Vault sync | all 3 machines | Synology Drive Client (two-way, on-demand disabled) |

## Repo layout

```
goBrain/
├── README.md                this file
├── CLAUDE.md                live context for Claude Code sessions
├── docs/
│   ├── architecture.md      design rationale and data model
│   ├── roadmap.md           day-by-day build plan and history
│   ├── runbook.md           operational runbook for deploy and daily ops
│   └── model-routing.md     which LLM does what and why
├── compose/postgres/        Docker Compose for NAS
│   ├── docker-compose.yml
│   ├── init.sql             schema (documents, chunks, pluto_events, ingestion_log)
│   └── .env.example
├── mac-mini/                LaunchAgents + setup scripts for the Mac
│   ├── setup-ollama.sh
│   ├── setup-llamacpp.sh
│   ├── setup-ingester.sh
│   └── launchd/             plist templates
├── ingester/                FastAPI ingester (pyproject + src)
├── mcp-server/              MCP stdio server for search_brain
├── windows/                 Claude Code shipper for Windows PC
│   ├── ship-claude-code.ps1
│   ├── ship-hidden.vbs
│   ├── install-shipper.ps1
│   └── README.md
└── scripts/                 registration helpers
```

## Quickstart — fresh install

Assuming NAS Container Manager is installed and SSH key auth is set up:

```bash
# 1. NAS — Postgres + pgvector
ssh $NAS_USER@$NAS_IP "mkdir -p /volume1/docker/brain-db"
scp compose/postgres/* $NAS_USER@$NAS_IP:/volume1/docker/brain-db/
ssh $NAS_USER@$NAS_IP "cd /volume1/docker/brain-db && cp .env.example .env && vim .env && sudo docker compose up -d"

# 2. Mac Mini — LLM stack
cd mac-mini
./setup-ollama.sh          # Ollama + Gemma 4 E2B, E4B + nomic-embed + LaunchAgent
./setup-llamacpp.sh        # llama.cpp + Qwen 3.5 35B-A3B MoE on demand

# 3. Mac Mini — ingester
cd ../ingester
cp .env.example .env       # edit: vault path, Postgres DSN
uv sync
cd ../mac-mini
./setup-ingester.sh        # installs LaunchAgent, verifies health

# 4. Mac or PC — MCP server (per machine)
cd ../mcp-server
cp .env.example .env       # edit: Postgres DSN, Ollama base URL, vault path
uv sync
claude mcp add brain --scope user -- uv run --directory $(pwd) brain-mcp

# 5. Windows — Claude Code shipper
cd ../windows
.\install-shipper.ps1      # registers the every-10-min Scheduled Task
```

Full step-by-step in [docs/runbook.md](docs/runbook.md).

## Common ops

**Drop an export to ingest:**
```bash
mv ~/Downloads/<claude-ai-or-grok-export>.zip ~/Brain/_inbox/
# Watcher picks it up within seconds; Gemma E4B summarizes each conversation.
```

**Check ingest progress:**
```bash
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
# on NAS
docker compose exec postgres psql -U brain -d brain -c \
  "SELECT source, count(*) FROM documents GROUP BY source ORDER BY count(*) DESC;"
```

**Backfill every historical Claude Code session (e.g., after a Postgres wipe):**
```bash
curl -X POST http://127.0.0.1:8765/admin/reingest/claude-code
# Runs in background by default. Watch ingester log for `reingest_*` events.
```

**Re-process everything in `_inbox/`:**
```bash
curl -X POST http://127.0.0.1:8765/admin/reingest/inbox
```

**Search from Claude Code (Mac or Windows):**
> Use search_brain to find what we decided about Postgres migration.

MCP tools exposed: `search_brain`, `recent_sessions`, `pluto_activity`, `get_document`.

## Known caveats

- **Synology Drive on-demand sync must be OFF** for the `Brain` Sync Task. When on, files sync as 0-byte placeholders on the Mac that Python can read but `zipfile.ZipFile` can't open properly (it seeks to EOF without triggering materialization). Flip it off in Drive Client → Selective Sync Settings → Sync Mode → uncheck "Enable On-demand Sync."
- **`MAX_LOADED_MODELS=1` causes rerank contention** during bulk ingestion. MCP server detects this via `/api/ps` and falls back to raw ANN top-K (no rerank) so search stays responsive. Full rerank runs when nothing else is contending.
- **Windows-shipped JSONLs require the dot-prefix toggle** ("Sync files and folders with the prefix '.'") in Drive Client Selective Sync. `.claude-code-sources` is dot-prefixed to hide it from Obsidian's sidebar.
- **Claude Code watcher does not re-scan on startup** — it only catches modified files. Use the `/admin/reingest/claude-code` endpoint to backfill historical sessions.

## License

Personal project. No license asserted yet.
