"""FastAPI entry point for the ingester.

Endpoints:
  POST /ingest/document        — generic, for remote shippers (e.g. Windows PC Claude Code)
  POST /ingest/pluto-event     — structured tool-call events from Pluto
  POST /admin/drain-buffer     — replay SQLite buffer into Postgres
  GET  /health                 — simple heartbeat + buffer stats

Plus long-running watcher tasks (Claude Code JSONL + _inbox folder).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import settings
from .db import buffer_size, drain_buffer, postgres_available
from .ollama_client import OllamaClient
from .watchers import claude_code as claude_code_watcher
from .watchers import inbox as inbox_watcher
from .writers import IngestInput, ingest_document, record_pluto_event


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


log = structlog.get_logger(__name__)


# --- Schemas -----------------------------------------------------------------
class DocumentIn(BaseModel):
    source: str
    source_id: str
    conversation_text: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    project: str | None = None
    model: str | None = None
    turn_count: int | None = None
    tool_call_count: int | None = None
    extra_frontmatter: dict[str, Any] | None = None


class PlutoEventIn(BaseModel):
    ts: datetime | None = None
    kind: str
    tool_name: str | None = None
    parent_session_id: str | None = None
    payload: dict[str, Any] | None = None


# --- App ---------------------------------------------------------------------
def create_app() -> FastAPI:
    _configure_logging()
    app = FastAPI(title="goBrain ingester", version="0.1.0")
    ollama = OllamaClient()
    watchers_stop = asyncio.Event()
    background: list[asyncio.Task] = []

    @app.on_event("startup")
    async def _start() -> None:
        log.info("ingester_start",
                 postgres=postgres_available(),
                 vault=str(settings.vault_path))
        if settings.watch_claude_code:
            background.append(asyncio.create_task(
                claude_code_watcher.run(ollama, watchers_stop,
                                        root=settings.claude_code_projects_dir),
                name="watcher-claude-code-primary",
            ))
            for idx, extra in enumerate(settings.claude_code_extra_dirs):
                background.append(asyncio.create_task(
                    claude_code_watcher.run(ollama, watchers_stop, root=extra),
                    name=f"watcher-claude-code-extra-{idx}",
                ))
        if settings.watch_inbox:
            background.append(asyncio.create_task(
                inbox_watcher.run(ollama, watchers_stop),
                name="watcher-inbox",
            ))

    @app.on_event("shutdown")
    async def _stop() -> None:
        log.info("ingester_stop")
        watchers_stop.set()
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await ollama.aclose()

    @app.get("/health")
    async def health() -> dict:
        docs, events = buffer_size()
        return {
            "ok": True,
            "postgres_configured": postgres_available(),
            "buffer": {"documents": docs, "pluto_events": events},
            "vault": str(settings.vault_path),
        }

    @app.post("/ingest/document")
    async def ingest_document_endpoint(doc: DocumentIn) -> dict:
        try:
            vault_path = await ingest_document(
                IngestInput(**doc.model_dump()),
                ollama,
            )
        except Exception as exc:
            log.exception("ingest_failed")
            raise HTTPException(status_code=500, detail=str(exc))
        return {"ok": True, "vault_path": str(vault_path)}

    @app.post("/ingest/pluto-event")
    async def ingest_pluto_event(ev: PlutoEventIn) -> dict:
        await record_pluto_event(ev.model_dump())
        return {"ok": True}

    @app.post("/admin/drain-buffer")
    async def drain_buffer_endpoint() -> dict:
        if not postgres_available():
            raise HTTPException(status_code=400, detail="Postgres not configured")
        docs, events = drain_buffer()
        return {"ok": True, "docs_written": docs, "events_written": events}

    return app


def run() -> None:
    uvicorn.run(
        "brain_ingester.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
