"""Retrieval: pgvector ANN → Gemma re-rank → top-N with diversity cap."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import httpx
import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from .config import settings

log = structlog.get_logger(__name__)

_engine = create_engine(settings.postgres_dsn, pool_pre_ping=True, future=True)
_Session = sessionmaker(bind=_engine, expire_on_commit=False)


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    vault_path: str
    source: str
    project: str | None
    started_at: str | None
    summary: str | None
    content: str
    score: float  # higher = better (post re-rank)


async def embed_query(query: str) -> list[float]:
    # Long timeout: during bulk ingestion, the 16GB Mac Mini aggressively swaps
    # between gemma4:e4b (summarization) and our embed/rerank models. A swap
    # easily exceeds 30s of wall time. 600s is generous but keeps search
    # reliable under load.
    async with httpx.AsyncClient(base_url=settings.ollama_base_url, timeout=600.0) as client:
        r = await client.post("/api/embed", json={
            "model": settings.model_embed,
            "input": [query],
        })
        r.raise_for_status()
        return r.json()["embeddings"][0]


def ann_candidates(
    query_embedding: Sequence[float],
    limit: int,
    sources: list[str] | None = None,
) -> list[Hit]:
    source_filter = ""
    params: dict = {"embedding": list(query_embedding), "limit": limit}
    if sources:
        source_filter = "WHERE d.source = ANY(:sources)"
        params["sources"] = sources

    sql = text(f"""
        SELECT
            c.id::text           AS chunk_id,
            c.document_id::text  AS document_id,
            d.vault_path         AS vault_path,
            d.source             AS source,
            d.project            AS project,
            d.started_at         AS started_at,
            d.summary            AS summary,
            c.content            AS content,
            1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        {source_filter}
        ORDER BY c.embedding <=> CAST(:embedding AS vector)
        LIMIT :limit
    """)

    with _Session() as session:
        rows = session.execute(sql, params).mappings().all()

    return [
        Hit(
            chunk_id=r["chunk_id"],
            document_id=r["document_id"],
            vault_path=r["vault_path"],
            source=r["source"],
            project=r["project"],
            started_at=r["started_at"].isoformat() if r["started_at"] else None,
            summary=r["summary"],
            content=r["content"],
            score=float(r["score"]),
        )
        for r in rows
    ]


async def rerank(query: str, hits: list[Hit], keep: int) -> list[Hit]:
    """Use Gemma E2B to re-rank ANN candidates by actual relevance to the query.

    Simple scoring prompt: ask the model for a 0-100 score per candidate;
    sort by score, apply diversity cap, trim to `keep`.
    """
    if not hits:
        return []

    async with httpx.AsyncClient(base_url=settings.ollama_base_url, timeout=600.0) as client:
        scores: list[float] = []
        for h in hits:
            prompt = (
                f"QUERY: {query}\n\n"
                f"CANDIDATE:\n{h.content[:1200]}\n\n"
                "How relevant is the candidate to the query? Reply with only a single integer "
                "from 0 to 100 and nothing else."
            )
            r = await client.post("/api/chat", json={
                "model": settings.model_rerank,
                "messages": [{"role": "user", "content": prompt}],
                "think": False,
                "stream": False,
                "options": {"num_ctx": 2048, "temperature": 0.0},
            })
            r.raise_for_status()
            content = r.json()["message"]["content"].strip()
            scores.append(_parse_score(content))

    # Combine ANN score and re-rank score; re-rank dominates.
    for h, s in zip(hits, scores):
        h.score = 0.8 * s / 100.0 + 0.2 * h.score

    hits.sort(key=lambda h: h.score, reverse=True)

    # Diversity: cap chunks per document
    out: list[Hit] = []
    per_doc: dict[str, int] = {}
    for h in hits:
        if per_doc.get(h.document_id, 0) >= settings.max_chunks_per_document:
            continue
        out.append(h)
        per_doc[h.document_id] = per_doc.get(h.document_id, 0) + 1
        if len(out) >= keep:
            break
    return out


def _parse_score(text_value: str) -> float:
    # Tolerate the model emitting extra prose despite instructions.
    import re
    m = re.search(r"\d+", text_value)
    if not m:
        return 0.0
    return max(0.0, min(100.0, float(m.group(0))))


async def search(query: str, limit: int, sources: list[str] | None) -> list[Hit]:
    embedding = await embed_query(query)
    candidates = ann_candidates(embedding, settings.search_candidates, sources)
    return await rerank(query, candidates, limit)


# --- Secondary tools --------------------------------------------------------
def recent_documents(n: int, source: str | None = None) -> list[dict]:
    params: dict = {"limit": n}
    source_clause = ""
    if source:
        source_clause = "WHERE source = :source"
        params["source"] = source
    sql = text(f"""
        SELECT vault_path, source, project, started_at, summary, tags
        FROM documents
        {source_clause}
        ORDER BY COALESCE(started_at, ingested_at) DESC
        LIMIT :limit
    """)
    with _Session() as session:
        rows = session.execute(sql, params).mappings().all()
    return [
        {
            "vault_path": r["vault_path"],
            "source": r["source"],
            "project": r["project"],
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "summary": r["summary"],
            "tags": list(r["tags"] or []),
        }
        for r in rows
    ]


def pluto_activity(since_iso: str | None, tool: str | None) -> list[dict]:
    params: dict = {}
    where: list[str] = []
    if since_iso:
        where.append("ts >= :since")
        params["since"] = since_iso
    if tool:
        where.append("tool_name = :tool")
        params["tool"] = tool
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = text(f"""
        SELECT ts, kind, tool_name, parent_session_id, payload, summary
        FROM pluto_events
        {where_sql}
        ORDER BY ts DESC
        LIMIT 500
    """)
    with _Session() as session:
        rows = session.execute(sql, params).mappings().all()
    return [
        {
            "ts": r["ts"].isoformat(),
            "kind": r["kind"],
            "tool_name": r["tool_name"],
            "parent_session_id": r["parent_session_id"],
            "payload": r["payload"],
            "summary": r["summary"],
        }
        for r in rows
    ]


def get_document_text(vault_path: str) -> str:
    abs_path = settings.vault_path / vault_path
    if not abs_path.exists():
        raise FileNotFoundError(vault_path)
    return abs_path.read_text(encoding="utf-8")
