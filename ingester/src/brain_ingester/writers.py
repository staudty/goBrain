"""Write pipeline: vault markdown + Postgres documents/chunks rows.

Idempotent: on duplicate (source, source_id), updates raw_hash if changed
and re-embeds chunks; otherwise skips.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import structlog
from sqlalchemy import select

from .chunker import chunk_text
from .config import settings
from .db import buffer_document, pg_session, postgres_available
from .models import Chunk, Document, IngestionLog
from .ollama_client import OllamaClient
from .summarizer import Summary, summarize_conversation

log = structlog.get_logger(__name__)


@dataclass
class IngestInput:
    source: str                # claude-code | claude-desktop | claude-ai | grok | inbox | telegram
    source_id: str             # stable id for dedup
    conversation_text: str     # cleaned, human-readable body (markdown ok)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    project: str | None = None
    model: str | None = None
    turn_count: int | None = None
    tool_call_count: int | None = None
    extra_frontmatter: dict | None = None  # merged in, caller wins


async def ingest_document(inp: IngestInput, ollama: OllamaClient) -> Path:
    """Full pipeline: summarize, embed, write vault note, persist DB rows.

    Returns the vault path of the written note.
    """
    raw_hash = hashlib.sha256(inp.conversation_text.encode("utf-8")).hexdigest()

    # 1. Summarize
    summary = await summarize_conversation(ollama, inp.conversation_text)

    # 2. Build frontmatter + body
    vault_path = _vault_path_for(inp)
    meta = {
        "source": inp.source,
        "source_id": inp.source_id,
        "started_at": inp.started_at.isoformat() if inp.started_at else None,
        "ended_at": inp.ended_at.isoformat() if inp.ended_at else None,
        "project": inp.project,
        "model": inp.model,
        "turn_count": inp.turn_count,
        "tool_calls": inp.tool_call_count,
        "tags": summary.tags,
        "summary": summary.summary,
        "raw_hash": raw_hash,
    }
    if inp.extra_frontmatter:
        meta.update(inp.extra_frontmatter)
    meta = {k: v for k, v in meta.items() if v is not None}

    body = _render_body(summary, inp.conversation_text)
    post = frontmatter.Post(body, **meta)

    # 3. Write vault markdown
    abs_path = settings.vault_path / vault_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    # 4. Chunk + embed
    chunks = chunk_text(inp.conversation_text)
    chunk_contents = [c.content for c in chunks]
    embeddings = await ollama.embed(settings.model_embed, chunk_contents) if chunks else []

    # 5. Persist
    document_payload = {
        "id": str(uuid.uuid4()),
        "source": inp.source,
        "source_id": inp.source_id,
        "vault_path": str(vault_path),
        "started_at": inp.started_at,
        "ended_at": inp.ended_at,
        "project": inp.project,
        "model": inp.model,
        "turn_count": inp.turn_count,
        "tool_call_count": inp.tool_call_count,
        "summary": summary.summary,
        "tags": summary.tags,
        "raw_hash": raw_hash,
        "chunks": [
            {"chunk_index": c.index, "content": c.content, "embedding": e}
            for c, e in zip(chunks, embeddings)
        ],
    }

    if postgres_available():
        _write_to_postgres(document_payload)
    else:
        buffer_document(inp.source, inp.source_id, document_payload)
        log.info("buffered_document", source=inp.source, source_id=inp.source_id,
                 buffer_reason="postgres_unavailable")

    return vault_path


def _vault_path_for(inp: IngestInput) -> Path:
    date = (inp.started_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    raw = inp.source_id.replace("/", "_").replace(" ", "_")
    # Cap at 100 chars of readable prefix, then always append an 8-char
    # content hash of the FULL source_id. This keeps filenames readable
    # for humans AND unique for the DB — two sub-agents of the same
    # parent session share a prefix but always hash differently.
    # Previously a naive [:60] truncation collided on any source_id
    # that shared a prefix beyond ~60 chars (classic case: subagents
    # whose identity lived entirely in the suffix).
    h = hashlib.sha1(inp.source_id.encode("utf-8")).hexdigest()[:8]
    safe_id = f"{raw[:100]}_{h}"
    slug = f"{date}_{safe_id}.md"
    if inp.project:
        safe_proj = inp.project.replace("/", "_").replace(" ", "_")
        return Path("sessions") / inp.source / safe_proj / slug
    return Path("sessions") / inp.source / slug


def _render_body(summary: Summary, conversation_text: str) -> str:
    parts: list[str] = []
    if summary.key_points:
        parts.append("## Key points\n")
        parts.extend(f"- {kp}" for kp in summary.key_points)
        parts.append("")
    parts.append("## Conversation\n")
    parts.append(conversation_text.rstrip())
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Postgres writer
# ---------------------------------------------------------------------------
def _write_to_postgres(doc: dict) -> None:
    now = datetime.now(timezone.utc)

    with pg_session() as session:
        existing = session.execute(
            select(Document).where(
                Document.source == doc["source"],
                Document.source_id == doc["source_id"],
            )
        ).scalar_one_or_none()

        if existing and existing.raw_hash == doc["raw_hash"]:
            session.add(IngestionLog(
                id=str(uuid.uuid4()), source=doc["source"], source_id=doc["source_id"],
                action="skipped_duplicate", raw_hash=doc["raw_hash"], ingested_at=now,
            ))
            session.commit()
            log.info("skipped_duplicate", source=doc["source"], source_id=doc["source_id"])
            return

        if existing:
            # Content changed — wipe chunks, update row in place.
            session.delete(existing)
            session.flush()

        document = Document(
            id=doc["id"],
            source=doc["source"],
            source_id=doc["source_id"],
            vault_path=doc["vault_path"],
            started_at=doc["started_at"],
            ended_at=doc["ended_at"],
            project=doc["project"],
            model=doc["model"],
            turn_count=doc["turn_count"],
            tool_call_count=doc["tool_call_count"],
            summary=doc["summary"],
            tags=doc["tags"],
            raw_hash=doc["raw_hash"],
            ingested_at=now,
        )
        session.add(document)

        for c in doc["chunks"]:
            session.add(Chunk(
                id=str(uuid.uuid4()),
                document_id=document.id,
                chunk_index=c["chunk_index"],
                content=c["content"],
                embedding=c["embedding"],
            ))

        session.add(IngestionLog(
            id=str(uuid.uuid4()), source=doc["source"], source_id=doc["source_id"],
            action="updated" if existing else "created",
            raw_hash=doc["raw_hash"], ingested_at=now,
        ))
        session.commit()
        log.info("wrote_document", source=doc["source"], source_id=doc["source_id"],
                 chunks=len(doc["chunks"]))


# ---------------------------------------------------------------------------
# Buffer replay hook (called by db.drain_buffer)
# ---------------------------------------------------------------------------
def replay_buffered_document(source: str, source_id: str, payload: dict) -> None:
    _write_to_postgres(payload)
