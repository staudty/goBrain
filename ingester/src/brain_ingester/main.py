"""FastAPI entry point for the ingester.

Endpoints:
  POST /ingest/document                 — generic, for remote shippers (e.g. Windows PC Claude Code)
  POST /admin/drain-buffer              — replay SQLite buffer into Postgres
  POST /admin/reingest/claude-code      — backfill every Claude Code JSONL we can see
  POST /admin/reingest/inbox            — re-process everything currently in _inbox/
  GET  /health                          — simple heartbeat + buffer stats

Plus long-running watcher tasks (Claude Code JSONL + _inbox folder).
"""
from __future__ import annotations

import asyncio
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
from .writers import IngestInput, ingest_document


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
        docs = buffer_size()
        return {
            "ok": True,
            "postgres_configured": postgres_available(),
            "buffer": {"documents": docs},
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

    @app.post("/admin/drain-buffer")
    async def drain_buffer_endpoint() -> dict:
        if not postgres_available():
            raise HTTPException(status_code=400, detail="Postgres not configured")
        docs = drain_buffer()
        return {"ok": True, "docs_written": docs}

    @app.post("/admin/reingest/claude-code")
    async def reingest_claude_code(background: bool = True) -> dict:
        """Walk every Claude Code projects directory we watch and re-ingest every
        JSONL file found, regardless of whether it's been ingested before.

        Dedup (source, source_id) + raw_hash comparison means already-done
        sessions with unchanged content are fast-skipped. Useful after a
        Postgres wipe, or for picking up sessions that existed before the
        ingester was installed (the live watcher only catches MODIFIED files).

        `background=True` (default): returns immediately, processing runs as
        an asyncio task. Watch the ingester log for `reingest_*` events.
        `background=False`: blocks until done; suitable for scripts with a
        long curl --max-time.
        """
        from .watchers.claude_code import _new_state, _ingest

        dirs = [settings.claude_code_projects_dir, *settings.claude_code_extra_dirs]
        dirs = [d for d in dirs if d.exists()]
        files = []
        for root in dirs:
            files.extend((root, p) for p in sorted(root.rglob("*.jsonl")))

        async def _runner():
            log.info("reingest_started", source="claude-code", files=len(files))
            stats = {"scanned": len(files), "ingested": 0, "failed": 0}
            for root, path in files:
                try:
                    state = _new_state(path, root)
                    await _ingest(state, ollama)
                    stats["ingested"] += 1
                except Exception as exc:
                    log.exception("reingest_file_failed", path=str(path), error=repr(exc))
                    stats["failed"] += 1
            log.info("reingest_finished", source="claude-code", **stats)

        if background:
            asyncio.create_task(_runner(), name="reingest-claude-code")
            return {"ok": True, "started": True, "files_queued": len(files)}

        await _runner()
        return {"ok": True, "files": len(files), "note": "see log for per-file results"}

    @app.post("/admin/reingest/inbox")
    async def reingest_inbox(background: bool = True) -> dict:
        """Re-process every file currently sitting in the inbox. Useful after
        a Postgres wipe to regenerate summaries/embeddings for dropped
        exports, or to recover from a partial failure mid-batch."""
        from .watchers.inbox import _handle as inbox_handle

        inbox = settings.inbox_path
        if not inbox.exists():
            raise HTTPException(status_code=400, detail=f"inbox path not found: {inbox}")
        files = sorted([p for p in inbox.iterdir() if p.is_file()])

        async def _runner():
            log.info("reingest_started", source="inbox", files=len(files))
            stats = {"scanned": len(files), "handled": 0, "failed": 0}
            for path in files:
                try:
                    await inbox_handle(path, ollama)
                    stats["handled"] += 1
                except Exception as exc:
                    log.exception("reingest_inbox_file_failed", path=str(path), error=repr(exc))
                    stats["failed"] += 1
            log.info("reingest_finished", source="inbox", **stats)

        if background:
            asyncio.create_task(_runner(), name="reingest-inbox")
            return {"ok": True, "started": True, "files_queued": len(files)}

        await _runner()
        return {"ok": True, "files": len(files), "note": "see log for per-file results"}

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
