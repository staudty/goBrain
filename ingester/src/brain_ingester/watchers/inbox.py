"""Watch the `_inbox/` folder for manually-dropped files.

Supported drop types:
- Plain markdown / text (ingested as a single document)
- ZIP exports — routed by inspecting zip contents:
    - Claude.ai: contains `conversations.json` at root
    - Grok (xAI): contains `prod-grok-backend.json` (buried under `ttl/30d/export_data/...`)
- Raw Grok JSON (just the prod-grok-backend.json file itself)

On successful ingestion, the source file is moved to _inbox/_processed/<date>/.
On parser error, the file stays in _inbox/ so it can be retried after a code fix.
"""
from __future__ import annotations

import asyncio
import shutil
import zipfile
from datetime import date
from pathlib import Path

import structlog
from watchfiles import awatch

from ..config import settings
from ..ollama_client import OllamaClient
from ..writers import IngestInput, ingest_document

log = structlog.get_logger(__name__)


async def run(ollama: OllamaClient, stop: asyncio.Event) -> None:
    inbox = settings.inbox_path
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "_processed").mkdir(exist_ok=True)

    # Process anything already sitting in the inbox at startup
    for path in sorted(inbox.iterdir()):
        if path.is_file():
            await _handle(path, ollama)

    log.info("inbox_watcher_start", path=str(inbox))
    async for changes in awatch(inbox, stop_event=stop, recursive=False):
        for _change, path_str in changes:
            path = Path(path_str)
            if path.is_file() and path.parent == inbox:
                await _handle(path, ollama)


async def _handle(path: Path, ollama: OllamaClient) -> None:
    log.info("inbox_handle_start", path=str(path), size=path.stat().st_size)
    try:
        count = 0
        ext = path.suffix.lower()

        if ext in (".md", ".txt"):
            text = path.read_text(encoding="utf-8", errors="replace")
            await ingest_document(
                IngestInput(source="inbox", source_id=path.name, conversation_text=text),
                ollama,
            )
            count = 1

        elif ext == ".zip":
            kind = _detect_zip_kind(path)
            log.info("inbox_zip_detected", path=str(path), kind=kind)
            if kind == "claude-ai":
                from ..parsers import claude_ai as parser
            elif kind == "grok":
                from ..parsers import grok as parser
            else:
                log.warning("inbox_zip_unknown_format", path=str(path))
                return  # leave in _inbox so it can be inspected
            for inp in parser.parse(path):
                await ingest_document(inp, ollama)
                count += 1

        elif ext == ".json":
            # Raw Grok backend JSON (someone extracted it from the zip)
            from ..parsers import grok as parser
            for inp in parser.parse(path):
                await ingest_document(inp, ollama)
                count += 1

        else:
            log.warning("inbox_unknown_type", path=str(path))
            return

        log.info("inbox_handle_done", path=str(path), documents=count)
        _archive(path)

    except Exception as exc:
        log.exception("inbox_handle_failed", path=str(path), error=repr(exc))


def _detect_zip_kind(path: Path) -> str | None:
    """Inspect ZIP member names to decide which parser to dispatch to."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as exc:
        log.error("inbox_bad_zip", path=str(path), error=repr(exc))
        raise
    if any(n.endswith("conversations.json") for n in names):
        return "claude-ai"
    if any("prod-grok-backend.json" in n for n in names):
        return "grok"
    return None


def _archive(path: Path) -> None:
    dest_dir = path.parent / "_processed" / date.today().isoformat()
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest_dir / path.name))
