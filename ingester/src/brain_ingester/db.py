"""Storage layer. Dual-mode: Postgres when configured, SQLite buffer otherwise.

The SQLite buffer mirrors the Postgres schema loosely — just enough to queue
ingested documents and chunks until Postgres is reachable, at which point
`drain_buffer()` replays them.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------
_engine = None
_SessionFactory: sessionmaker[Session] | None = None


def postgres_available() -> bool:
    return bool(settings.postgres_dsn)


def _ensure_engine():
    global _engine, _SessionFactory
    if _engine is None and postgres_available():
        _engine = create_engine(settings.postgres_dsn, pool_pre_ping=True, future=True)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)


@contextmanager
def pg_session() -> Iterator[Session]:
    _ensure_engine()
    if _SessionFactory is None:
        raise RuntimeError("Postgres not configured; check BRAIN_POSTGRES_DSN")
    with _SessionFactory() as s:
        yield s


# ---------------------------------------------------------------------------
# SQLite fallback buffer
# ---------------------------------------------------------------------------
_BUFFER_SCHEMA = """
CREATE TABLE IF NOT EXISTS buffered_documents (
  source       TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  queued_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS buffered_pluto_events (
  ts           TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  queued_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _buffer_conn() -> sqlite3.Connection:
    settings.fallback_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.fallback_sqlite_path)
    conn.executescript(_BUFFER_SCHEMA)
    return conn


def buffer_document(source: str, source_id: str, payload: dict) -> None:
    """Queue a document for later replay into Postgres."""
    with _buffer_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO buffered_documents (source, source_id, payload_json) VALUES (?, ?, ?)",
            (source, source_id, json.dumps(payload, default=str)),
        )


def buffer_pluto_event(ts: str, payload: dict) -> None:
    with _buffer_conn() as conn:
        conn.execute(
            "INSERT INTO buffered_pluto_events (ts, payload_json) VALUES (?, ?)",
            (ts, json.dumps(payload, default=str)),
        )


def buffer_size() -> tuple[int, int]:
    with _buffer_conn() as conn:
        (docs,) = conn.execute("SELECT COUNT(*) FROM buffered_documents").fetchone()
        (events,) = conn.execute("SELECT COUNT(*) FROM buffered_pluto_events").fetchone()
    return docs, events


def drain_buffer() -> tuple[int, int]:
    """Replay buffered docs + events into Postgres. Returns (docs_written, events_written)."""
    if not postgres_available():
        log.warning("drain_buffer called but Postgres not configured")
        return 0, 0

    # Actual replay is implemented alongside the writer functions (see writers.py).
    # Placeholder here; Phase 6 (Thursday) wires it up.
    from .writers import replay_buffered_document, replay_buffered_pluto_event

    docs_written = events_written = 0
    with _buffer_conn() as conn:
        for source, source_id, payload_json in conn.execute(
            "SELECT source, source_id, payload_json FROM buffered_documents"
        ):
            try:
                replay_buffered_document(source, source_id, json.loads(payload_json))
                conn.execute(
                    "DELETE FROM buffered_documents WHERE source = ? AND source_id = ?",
                    (source, source_id),
                )
                docs_written += 1
            except Exception as exc:
                log.error("replay_failed", source=source, source_id=source_id, error=str(exc))

        for (ts, payload_json) in conn.execute(
            "SELECT ts, payload_json FROM buffered_pluto_events ORDER BY ts"
        ).fetchall():
            try:
                replay_buffered_pluto_event(ts, json.loads(payload_json))
                events_written += 1
            except Exception as exc:
                log.error("pluto_replay_failed", ts=ts, error=str(exc))
        conn.execute("DELETE FROM buffered_pluto_events")

    return docs_written, events_written
