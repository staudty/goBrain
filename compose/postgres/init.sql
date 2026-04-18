-- goBrain schema
-- Run automatically by the postgres container on first init via docker-entrypoint-initdb.d.
-- Idempotent so it's safe to re-run.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy text matches for fallback search
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- --------------------------------------------------------------------------
-- Documents: one row per ingested conversation or artifact.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
  id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  source           TEXT NOT NULL,
  source_id        TEXT NOT NULL,
  vault_path       TEXT NOT NULL UNIQUE,
  started_at       TIMESTAMPTZ,
  ended_at         TIMESTAMPTZ,
  project          TEXT,
  model            TEXT,
  turn_count       INTEGER,
  tool_call_count  INTEGER,
  summary          TEXT,
  tags             TEXT[] DEFAULT '{}',
  raw_hash         TEXT,
  ingested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT documents_source_id_unique UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS documents_source_started_idx
  ON documents (source, started_at DESC);
CREATE INDEX IF NOT EXISTS documents_started_idx
  ON documents (started_at DESC);
CREATE INDEX IF NOT EXISTS documents_tags_idx
  ON documents USING GIN (tags);
CREATE INDEX IF NOT EXISTS documents_summary_trgm_idx
  ON documents USING GIN (summary gin_trgm_ops);

-- --------------------------------------------------------------------------
-- Chunks: retrieval unit. Each document is split into ~500-token chunks.
-- nomic-embed-text produces 768-dim vectors.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index   INTEGER NOT NULL,
  content       TEXT NOT NULL,
  embedding     vector(768),
  CONSTRAINT chunks_doc_idx_unique UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_document_idx
  ON chunks (document_id);

-- HNSW index for ANN cosine search. m=16, ef_construction=64 are solid defaults
-- for corpora in the 10k-1M chunk range; retune if we outgrow.
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
  ON chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- --------------------------------------------------------------------------
-- Pluto events: every tool call, every message, every error.
-- Fine-grained so we can answer "what did Pluto do?" at any resolution.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pluto_events (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  ts                 TIMESTAMPTZ NOT NULL,
  kind               TEXT NOT NULL,
  tool_name          TEXT,
  parent_session_id  TEXT,
  payload            JSONB,
  summary            TEXT,
  ingested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS pluto_events_ts_idx
  ON pluto_events (ts DESC);
CREATE INDEX IF NOT EXISTS pluto_events_tool_idx
  ON pluto_events (tool_name) WHERE tool_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS pluto_events_kind_idx
  ON pluto_events (kind);
CREATE INDEX IF NOT EXISTS pluto_events_session_idx
  ON pluto_events (parent_session_id) WHERE parent_session_id IS NOT NULL;

-- --------------------------------------------------------------------------
-- Ingestion log: audit trail of what we ingested, when, from where, with hash
-- for idempotency and drift detection.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_log (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  source        TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  action        TEXT NOT NULL,       -- created | updated | skipped_duplicate | failed
  error         TEXT,
  raw_hash      TEXT,
  ingested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ingestion_log_time_idx
  ON ingestion_log (ingested_at DESC);
CREATE INDEX IF NOT EXISTS ingestion_log_source_idx
  ON ingestion_log (source, source_id);
