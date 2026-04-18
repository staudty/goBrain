"""SQLAlchemy models. Mirrors compose/postgres/init.sql."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    ForeignKey,
    Integer,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import settings


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("source", "source_id", name="documents_source_id_unique"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    vault_path: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    project: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    turn_count: Mapped[int | None] = mapped_column(Integer)
    tool_call_count: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    raw_hash: Mapped[str | None] = mapped_column(String)
    ingested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="chunks_doc_idx_unique"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embed_dim))

    document: Mapped[Document] = relationship(back_populates="chunks")


class PlutoEvent(Base):
    __tablename__ = "pluto_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String)
    parent_session_id: Mapped[str | None] = mapped_column(String)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))


class IngestionLog(Base):
    __tablename__ = "ingestion_log"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    raw_hash: Mapped[str | None] = mapped_column(String)
    ingested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
