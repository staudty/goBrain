# Model routing

Who does what. Applies the pattern from leopardracer's published Mac Mini stack to our specific needs.

## Tiers

| Tier | Model | Host | Port | Residency | Invoked for |
|---|---|---|---|---|---|
| Fast | `gemma4:e2b` (2.3B eff.) | Ollama on Mac Mini | 11434 | Paged in/out by Ollama (10-min idle) | Re-ranking search candidates; classifying incoming events; short responses |
| Primary | `gemma4:e4b` (4.5B eff.) | Ollama on Mac Mini | 11434 | Same slot as Fast (`MAX_LOADED_MODELS=1`) | Summarizing conversations on ingest; generating vault note bodies; context compression before Claude calls |
| Heavy | `qwen3.5:35b-a3b` UD-IQ3_XXS (MoE, ~3B active) | llama.cpp on Mac Mini | 8081 | `--mmap` pages from SSD on demand | Nightly Pluto-activity rollup; whole-day signal compression; Claude-fallback responses when Claude is rate-limited |
| Embed | `nomic-embed-text` (768d) | Ollama on Mac Mini | 11434 | Tiny, always warm | Every chunk at ingest; every query at search |
| Expert | Claude (`claude-opus-4-7`, `claude-sonnet-4-6`) | Anthropic API | — | Remote | User-initiated reasoning; tight Gemma-curated context only |

## Always-on thinking-mode discipline

Gemma and Qwen both ship with "thinking mode." **We disable it** for every call in this system.

- Summarization: `think: false` — structured output, no reasoning needed
- Classification: `think: false` — one-word answers
- Re-ranking: `think: false` — numeric score

This is the 30× speedup from leopardracer's write-up. The only place we would enable thinking is a free-form complex reasoning task, which we don't have here — that's what Claude is for.

## Concrete routing rules

### On conversation ingestion

1. Ingester receives a completed conversation (Claude Code, Desktop, etc.)
2. Call **E4B** for `summarize_conversation(text)` → summary + tags + key points
3. Call **nomic-embed-text** for each ~500-token chunk → 768-dim vectors
4. Persist to Postgres + vault
5. **Never** calls the 35B — waste of RAM-paging cost for a bounded summarization task

### On `search_brain(query)` from any MCP client

1. **nomic-embed-text** embeds the query
2. pgvector ANN top-20
3. **E2B** re-ranks (per-candidate 0-100 score, low-temp, `think: false`)
4. Return top-5 with diversity cap

### Nightly, 02:00 local

1. `launchctl load com.gobag.llamacpp` — start 35B heavy tier
2. Pull the day's `pluto_events` rows
3. **Qwen 35B MoE** compresses raw events into a dense rollup markdown note
4. Ingest that note (summarization pass via E4B for the outer document)
5. `launchctl unload com.gobag.llamacpp` — release the heavy tier

### On Claude rate-limit or timeout (from Pluto)

1. Pluto catches the error
2. Pluto POSTs the same prompt to `http://mac-mini:8081/v1/chat/completions` (llama.cpp 35B)
3. Response is tagged `[Local Fallback]` in Pluto's reply
4. Logged to `pluto_events` for morning review

## Why not use the 35B as the librarian?

Because leopardracer's numbers assume the 35B is on-demand. Holding it resident via `--mmap` with zero idle wastes VRAM-equivalent page cache that macOS would otherwise use for everything else. Summarization and re-ranking run constantly; paying the 35B's first-token latency for those is bad. E4B does those tasks well at 30+ tok/s with the model already hot.

The 35B earns its keep on rare, heavy tasks where quality matters and latency is tolerable (nightly compression, emergency fallback).

## Claude discipline

The system never passes raw vault content to Claude unless the caller explicitly invokes `get_document(vault_path)`. Default search flow returns ~2.5K tokens of curated chunks. For a typical "what did we decide about X?" query:

- Query cost: 1 embedding (nomic), 20 ANN lookups (Postgres), 20 re-rank calls (E2B)
- Payload to Claude: ~2.5K tokens
- No Claude tokens spent on scoring, summarizing, or traversal

This is the whole point. Gemma is the librarian; Claude is the expert; the user's Claude Max budget stays focused on actual thinking.
