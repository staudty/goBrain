"""FastAPI entry point for the ingester.

Endpoints:
  GET  /dashboard                       — zero-build status dashboard (HTML)
  GET  /stats/counts                    — totals per source + grand total + buffer
  GET  /stats/recent                    — recent documents for the recency table
  GET  /stats/timeline                  — hourly ingest counts for the last 24h
  POST /ingest/document                 — generic, for remote shippers
  POST /admin/drain-buffer              — replay SQLite buffer into Postgres
  POST /admin/reingest/claude-code      — backfill every Claude Code JSONL we can see
  POST /admin/reingest/inbox            — re-process everything currently in _inbox/
  GET  /health                          — simple heartbeat + buffer stats

Plus long-running watcher tasks (Claude Code JSONL + _inbox folder).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text

from .config import settings
from .db import buffer_size, drain_buffer, pg_session, postgres_available
from .models import Document
from .ollama_client import OllamaClient
from .watchers import claude_code as claude_code_watcher
from .watchers import inbox as inbox_watcher
from .writers import IngestInput, ingest_document

_DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


def _count_grok_conversations(zip_path: Path) -> int | None:
    """Return number of conversations inside a Grok export zip, or None if
    it isn't a Grok zip or can't be read."""
    import json as _json
    import zipfile as _zipfile

    try:
        with _zipfile.ZipFile(zip_path) as zf:
            name = next(
                (n for n in zf.namelist() if n.endswith("prod-grok-backend.json")),
                None,
            )
            if not name:
                return None
            with zf.open(name) as f:
                data = _json.load(f)
        return len(data.get("conversations") or [])
    except (OSError, _zipfile.BadZipFile, _json.JSONDecodeError, KeyError):
        return None


def _count_claude_ai_conversations(zip_path: Path) -> int | None:
    """Return number of conversations inside a Claude.ai export zip, or
    None if it isn't one. Claude.ai exports have a flat `conversations.json`
    at the zip root (alongside users/projects/memories)."""
    import json as _json
    import zipfile as _zipfile

    try:
        with _zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            if "conversations.json" not in names:
                return None
            with zf.open("conversations.json") as f:
                data = _json.load(f)
        return len(data) if isinstance(data, list) else None
    except (OSError, _zipfile.BadZipFile, _json.JSONDecodeError, KeyError):
        return None


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

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        if not _DASHBOARD_HTML.exists():
            raise HTTPException(status_code=500, detail="dashboard.html missing from package")
        return HTMLResponse(_DASHBOARD_HTML.read_text(encoding="utf-8"))

    @app.get("/stats/counts")
    async def stats_counts() -> dict:
        buf = buffer_size()
        if not postgres_available():
            return {
                "postgres_configured": False,
                "total": 0,
                "by_source": [],
                "buffer": buf,
            }
        with pg_session() as s:
            rows = s.execute(
                select(Document.source, func.count(Document.id))
                .group_by(Document.source)
                .order_by(func.count(Document.id).desc())
            ).all()
            total = s.execute(select(func.count(Document.id))).scalar_one()
            since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
            last_24h = s.execute(
                select(func.count(Document.id)).where(Document.ingested_at >= since)
            ).scalar_one()
        return {
            "postgres_configured": True,
            "total": total,
            "last_24h": last_24h,
            "by_source": [{"source": src, "count": n} for src, n in rows],
            "buffer": buf,
        }

    @app.get("/stats/recent")
    async def stats_recent(limit: int = 25) -> dict:
        if not postgres_available():
            return {"postgres_configured": False, "items": []}
        limit = max(1, min(limit, 100))
        with pg_session() as s:
            rows = s.execute(
                select(
                    Document.source,
                    Document.project,
                    Document.vault_path,
                    Document.summary,
                    Document.ingested_at,
                    Document.turn_count,
                )
                .order_by(Document.ingested_at.desc())
                .limit(limit)
            ).all()
        return {
            "postgres_configured": True,
            "items": [
                {
                    "source": r.source,
                    "project": r.project,
                    "vault_path": r.vault_path,
                    "summary": r.summary,
                    "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None,
                    "turn_count": r.turn_count,
                }
                for r in rows
            ],
        }

    @app.get("/stats/system")
    async def stats_system() -> dict:
        """System health: everything upstream + downstream of this process."""
        result: dict[str, Any] = {"ingester": {"ok": True}}

        # Postgres
        pg_status: dict[str, Any] = {"configured": postgres_available(), "buffered": buffer_size()}
        if postgres_available():
            try:
                with pg_session() as s:
                    s.execute(text("SELECT 1"))
                pg_status["ok"] = True
            except Exception as exc:
                pg_status["ok"] = False
                pg_status["error"] = str(exc)[:200]
        else:
            pg_status["ok"] = False
        result["postgres"] = pg_status

        # Ollama — tags (installed models) + ps (currently loaded)
        ollama_status: dict[str, Any] = {"base_url": settings.ollama_base_url}
        expected = [settings.model_fast, settings.model_primary, settings.model_embed]
        ollama_status["expected_models"] = expected
        try:
            async with httpx.AsyncClient(timeout=3.0) as h:
                tags_r = await h.get(f"{settings.ollama_base_url}/api/tags")
                tags_r.raise_for_status()
                installed = {m.get("name") or m.get("model") for m in tags_r.json().get("models", [])}
                installed.discard(None)
                ps_r = await h.get(f"{settings.ollama_base_url}/api/ps")
                ps_r.raise_for_status()
                loaded = [m.get("name") or m.get("model") for m in ps_r.json().get("models", [])]
            missing = [m for m in expected if m not in installed and f"{m}:latest" not in installed]
            ollama_status["ok"] = not missing
            ollama_status["installed_count"] = len(installed)
            ollama_status["missing"] = missing
            ollama_status["loaded_now"] = loaded
        except Exception as exc:
            ollama_status["ok"] = False
            ollama_status["error"] = str(exc)[:200]
        result["ollama"] = ollama_status

        # llama.cpp — optional heavy tier; 'ok' only when we can reach it
        llama_status: dict[str, Any] = {"base_url": settings.llamacpp_base_url, "optional": True}
        try:
            async with httpx.AsyncClient(timeout=2.0) as h:
                r = await h.get(f"{settings.llamacpp_base_url}/v1/models")
                r.raise_for_status()
            llama_status["ok"] = True
        except Exception:
            llama_status["ok"] = False
        result["llamacpp"] = llama_status

        # Watchers — asyncio tasks we spawned at startup
        watcher_rows = []
        for t in background:
            watcher_rows.append({
                "name": t.get_name(),
                "ok": not t.done(),
                "error": (repr(t.exception()) if t.done() and not t.cancelled() and t.exception() else None),
            })
        result["watchers"] = watcher_rows

        # Windows shipper — freshness of the synced Claude Code directory.
        # We can't observe the scheduled task from here, only its output. So
        # "ok" means "we see files from the shipper"; the age is purely
        # informational since an idle user produces no new finished sessions.
        shipper_status: dict[str, Any] = {}
        extra_dirs = list(settings.claude_code_extra_dirs)
        if extra_dirs:
            latest_mtime = 0.0
            latest_path = None
            file_count = 0
            for d in extra_dirs:
                if not d.exists():
                    continue
                for p in d.rglob("*.jsonl"):
                    file_count += 1
                    try:
                        m = p.stat().st_mtime
                    except OSError:
                        continue
                    if m > latest_mtime:
                        latest_mtime = m
                        latest_path = p
            shipper_status["watched_dirs"] = [str(d) for d in extra_dirs]
            shipper_status["files_seen"] = file_count
            shipper_status["ok"] = file_count > 0
            if latest_mtime:
                age = datetime.now(tz=timezone.utc).timestamp() - latest_mtime
                shipper_status["last_sync_age_seconds"] = int(age)
                shipper_status["last_sync_iso"] = datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat()
                shipper_status["last_file"] = str(latest_path.name) if latest_path else None
            else:
                shipper_status["note"] = "no JSONLs observed in watched dirs"
        else:
            shipper_status["ok"] = True
            shipper_status["note"] = "no extra Claude Code dirs configured"
        result["windows_shipper"] = shipper_status

        # Overall roll-up
        required_ok = (
            pg_status["ok"]
            and ollama_status.get("ok")
            and all(w["ok"] for w in watcher_rows)
        )
        result["overall_ok"] = bool(required_ok)

        return result

    @app.get("/stats/progress")
    async def stats_progress() -> dict:
        """Per-source progress: what's ingested vs what's still pending.

        Fast: counts JSONL files on disk, counts files in _inbox/, parses
        Grok zips' backend JSON for conversation counts. No embedding or
        LLM calls.
        """
        out: dict[str, Any] = {"sources": []}
        if not postgres_available():
            return {"postgres_configured": False, "sources": []}

        # Claude Code: compare JSONL files on disk to documents in DB.
        cc_dirs = [settings.claude_code_projects_dir, *settings.claude_code_extra_dirs]
        on_disk = 0
        for d in cc_dirs:
            if d.exists():
                on_disk += sum(1 for _ in d.rglob("*.jsonl"))
        with pg_session() as s:
            cc_ingested = s.execute(
                select(func.count(Document.id)).where(Document.source == "claude-code")
            ).scalar_one()
        cc = {
            "source": "claude-code",
            "ingested": cc_ingested,
            "total": max(cc_ingested, on_disk),
            "pending_estimate": max(0, on_disk - cc_ingested),
        }
        if cc["total"]:
            cc["pct"] = round(100 * cc_ingested / cc["total"])
        else:
            cc["pct"] = 100
        out["sources"].append(cc)

        # Inbox (Grok + Claude.ai exports + ad-hoc files):
        # count files still in _inbox/ vs files already in _processed/.
        inbox = settings.inbox_path
        pending_files = []
        processed_files = 0
        if inbox.exists():
            for p in inbox.iterdir():
                if p.is_file():
                    pending_files.append(p)
            processed_root = inbox / "_processed"
            if processed_root.exists():
                processed_files = sum(1 for _ in processed_root.rglob("*") if _.is_file())

        out["inbox"] = {
            "pending_files": len(pending_files),
            "processed_files": processed_files,
            "pending_names": [p.name for p in pending_files],
        }

        # Inbox-zip backfill: count remaining conversations inside each
        # pending zip so the bar shows conversation-level progress rather
        # than "1 file left". Grok and Claude.ai have different zip shapes;
        # dispatch per file.
        pending_grok = 0
        pending_claude_ai = 0
        grok_details: list[dict] = []
        claude_ai_details: list[dict] = []
        for p in pending_files:
            if p.suffix.lower() != ".zip":
                continue
            g = _count_grok_conversations(p)
            if g is not None:
                grok_details.append({"file": p.name, "conversations": g})
                pending_grok += g
                continue
            c = _count_claude_ai_conversations(p)
            if c is not None:
                claude_ai_details.append({"file": p.name, "conversations": c})
                pending_claude_ai += c

        with pg_session() as s:
            grok_ingested = s.execute(
                select(func.count(Document.id)).where(Document.source == "grok")
            ).scalar_one()
            claude_ai_ingested = s.execute(
                select(func.count(Document.id)).where(Document.source == "claude-ai")
            ).scalar_one()

        grok = {
            "source": "grok",
            "ingested": grok_ingested,
            "pending_in_zips": pending_grok,
            "zip_details": grok_details,
        }
        grok_total = grok_ingested + pending_grok
        grok["total"] = grok_total
        grok["pct"] = round(100 * grok_ingested / grok_total) if grok_total else 100
        out["sources"].append(grok)

        claude_ai = {
            "source": "claude-ai",
            "ingested": claude_ai_ingested,
            "pending_in_zips": pending_claude_ai,
            "zip_details": claude_ai_details,
        }
        ca_total = claude_ai_ingested + pending_claude_ai
        claude_ai["total"] = ca_total
        claude_ai["pct"] = round(100 * claude_ai_ingested / ca_total) if ca_total else 100
        out["sources"].append(claude_ai)

        return out

    @app.get("/stats/timeline")
    async def stats_timeline(hours: int = 24) -> dict:
        """Hourly ingest counts over the last N hours (default 24, max 168)."""
        if not postgres_available():
            return {"postgres_configured": False, "buckets": []}
        hours = max(1, min(hours, 168))
        since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        with pg_session() as s:
            rows = s.execute(
                text(
                    """
                    SELECT date_trunc('hour', ingested_at) AS bucket, count(*) AS n
                    FROM documents
                    WHERE ingested_at >= :since
                    GROUP BY bucket
                    ORDER BY bucket ASC
                    """
                ),
                {"since": since},
            ).all()
        return {
            "postgres_configured": True,
            "buckets": [
                {"bucket": r.bucket.isoformat() if r.bucket else None, "count": r.n}
                for r in rows
            ],
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
