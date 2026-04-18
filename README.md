# goBrain

A self-hosted "second brain" that captures every AI conversation and every meaningful action across Chris's stack, indexes it, and exposes it as a single searchable context surface to Claude Code, Claude Desktop, Pluto (Telegram bot on OpenClaw), Grok, and anything else that speaks MCP.

## Why

AI conversations are siloed across Claude Code sessions, Claude Desktop, Claude.ai web, Pluto on Telegram, and Grok on iPhone. None of them share memory. This repo unifies them into one vault + one vector index, queryable by all of them.

## Architecture, one paragraph

The **vault** is a folder of markdown files on the Synology NAS, synced to PC and Mac Mini via Synology Drive and browsed with **Obsidian**. A **Postgres + pgvector** database runs in Docker on the NAS and indexes everything. On the Mac Mini, **Ollama** hosts small Gemma 4 models (E2B for fast classification, E4B for summarization) and **llama.cpp** hosts a 35B MoE model (Qwen 3.5 35B-A3B via `--mmap`) for heavy compression and Claude-fallback. An **ingester** service watches for new conversations from every source (Claude Code JSONL logs, Desktop chats, `_inbox/` folder for manual exports, Pluto tool calls, Telegram) and writes markdown into the vault plus embeddings into pgvector. An **MCP server** exposes `search_brain()` and friends to any AI client. Claude is only called with tight, Gemma-curated context — never the raw vault.

See [docs/architecture.md](docs/architecture.md) for the long version.

## Hosts and what runs where

| Host | Runs |
|---|---|
| Synology NAS (DS224+, `192.168.1.178`) | Vault storage; Postgres + pgvector (Docker) |
| Mac Mini M4 (16GB) | Ollama (E2B + E4B + nomic-embed-text); llama.cpp (Qwen 35B-A3B MoE on `--mmap`); ingester FastAPI; local MCP server |
| Windows PC | Obsidian; tiny Claude Code log shipper → Mac Mini ingester; local MCP server for Claude Code on Windows |
| Raspberry Pi | (unchanged — AdGuard, Plex, *arr) |
| Linode VPS `gobag.dev` | (unchanged — SearXNG) |

## Quickstart (summary — full steps in `docs/runbook.md`)

**On NAS (after RAM upgrade arrives):**
```bash
cd /volume1/docker/brain-db
docker compose up -d
```

**On Mac Mini:**
```bash
cd ~/goBrain/mac-mini
./setup-ollama.sh
./setup-llamacpp.sh
launchctl load ~/Library/LaunchAgents/com.gobag.ollama.plist
cd ../ingester
uv sync && uv run brain-ingester
```

**On both Mac Mini and Windows PC:**
- Install Obsidian, open the Synology-Drive-synced `Brain/` folder as a vault.
- Install the MCP server locally and register it with Claude Code / Claude Desktop.

## Status

Active build. See [docs/roadmap.md](docs/roadmap.md) for the day-by-day plan.
