# Model routing

Who does what, and why.

## Tiers

| Tier | Model | Host | Port | Residency | Invoked for |
|---|---|---|---|---|---|
| Fast | `gemma4:e2b` (2.3B eff.) | Ollama on always-on host | 11434 | Paged in/out by Ollama (60-min idle) | Reranking search candidates; classifying incoming events; short responses |
| Primary | `gemma4:e4b` (4.5B eff.) | Ollama on always-on host | 11434 | Same slot as Fast (`MAX_LOADED_MODELS=1`) | Summarizing conversations on ingest; generating vault note bodies; context compression before cloud-LLM calls |
| Heavy (optional) | `qwen3.5:35b-a3b` UD-IQ3_XXS (MoE, ~3B active) | llama.cpp on always-on host | 8081 | `--mmap` pages from SSD on demand | Batch compression; cloud-fallback responses when your cloud LLM is rate-limited |
| Embed | `nomic-embed-text` (768d) | Ollama on always-on host | 11434 | Tiny, always warm | Every chunk at ingest; every query at search |
| Expert (optional) | Any cloud LLM (e.g. Claude) | External API | — | Remote | User-initiated reasoning with tight local-curated context only |

## Always-on thinking-mode discipline

Gemma and Qwen both ship with "thinking mode." **We disable it** for every call in this system.

- Summarization: `think: false` — structured output, no reasoning needed
- Classification: `think: false` — one-word answers
- Reranking: `think: false` — numeric score

This is typically a 10-30× speedup. The only place you would enable thinking is a free-form complex reasoning task, which we don't have here — that's what the expert tier is for.

## Concrete routing rules

### On conversation ingestion

1. Ingester receives a completed conversation (Claude Code, Desktop, Grok export, etc.)
2. Call **E4B** for `summarize_conversation(text)` → summary + tags + key points
3. Call **nomic-embed-text** for each ~500-token chunk → 768-dim vectors
4. Persist to Postgres + vault
5. **Never** calls the 35B — waste of RAM-paging cost for a bounded summarization task

### On `search_brain(query)` from any MCP client

1. **nomic-embed-text** embeds the query
2. pgvector ANN top-K
3. **E2B** reranks (per-candidate 0-100 score, low-temp, `think: false`)
   - If the summarizer (E4B) is currently pinned by ingestion, skip rerank and return raw ANN top-K with a diversity cap. The MCP server detects contention via Ollama's `/api/ps`.
4. Return top-N

### On expert-tier rate-limit or timeout (optional)

If you've configured a heavy-tier fallback, your agent catches the error and POSTs the same prompt to `http://<always-on-host>:8081/v1/chat/completions`. Response is tagged `[Local Fallback]` in the reply.

## Why not use the 35B as the librarian?

Because holding it resident via `--mmap` with zero idle wastes page cache the OS would otherwise use for everything else. Summarization and reranking run constantly; paying the 35B's first-token latency for those is bad. E4B does those tasks well with the model already hot.

The 35B earns its keep on rare, heavy tasks where quality matters and latency is tolerable (batch compression, emergency fallback).

## Cloud-LLM discipline

The system never passes raw vault content to a cloud LLM unless the caller explicitly invokes `get_document(vault_path)`. Default search flow returns ~2.5K tokens of curated chunks. For a typical "what did we decide about X?" query:

- Query cost: 1 embedding (nomic), K ANN lookups (Postgres), K rerank calls (E2B)
- Payload to cloud LLM: ~2.5K tokens
- No cloud tokens spent on scoring, summarizing, or traversal

This is the whole point. The small local models are the librarian; the cloud LLM is the expert; your cloud-LLM budget stays focused on actual thinking.

## Qwen 3.5 thinking mode — server-level disable

Qwen 3.5 35B-A3B has "thinking" (reasoning) mode baked into its chat template. Without it off, the model burns every output token on internal chain-of-thought before producing anything visible, tanking throughput and making `content` empty. llama.cpp's server exposes two ways to disable it:

- **Global (recommended):** launch llama-server with `--reasoning off`. Applies to every request regardless of client.
- **Per-request:** pass `{"chat_template_kwargs": {"enable_thinking": false}}` in the API body. Required when calling a server that doesn't have `--reasoning off` baked in.

The reference LaunchAgent at `mac-mini/launchd/com.gobag.llamacpp.plist` bakes in `--reasoning off`.

## Throughput notes

- Expect ~8 tok/s cold on a base M4 Mac Mini 16GB running Qwen 35B-A3B UD-IQ3_XXS with `-ngl 0`, 4K context, 10 threads.
- The bottleneck is **SSD read bandwidth** paging expert weights for each token, not CPU compute (so `--threads 10` vs `--threads 4` makes little difference).
- Throughput trends upward over days/weeks as the OS page cache learns the working set of frequently-activated experts.
- Acceptable for heavy-tier use cases (batch compression, rare fallback). Not suitable as an interactive model.
