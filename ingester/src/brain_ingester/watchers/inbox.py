"""Watch the `_inbox/` folder for manually-dropped files.

Supported drop types (detected by filename/content):
- Grok export: xAI JSON or ZIP
- Claude.ai export: Anthropic ZIP
- Plain markdown / text / PDF (ingested as a single document)

On successful ingestion, the source file is moved to _inbox/_processed/<date>/.
"""
from __future__ import annotations

import asyncio
import shutil
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
    try:
        if path.suffix.lower() in (".md", ".txt"):
            text = path.read_text(encoding="utf-8", errors="replace")
            await ingest_document(
                IngestInput(
                    source="inbox",
                    source_id=path.name,
                    conversation_text=text,
                ),
                ollama,
            )
        elif path.suffix.lower() == ".json":
            # Heuristic: xAI Grok export; parser implementation lives in
            # a dedicated module (parsers.grok) populated in Phase 4.
            from ..parsers import grok as grok_parser
            for inp in grok_parser.parse(path):
                await ingest_document(inp, ollama)
        elif path.suffix.lower() == ".zip":
            # Heuristic: Claude.ai export. Same deal — Phase 4.
            from ..parsers import claude_ai as claude_ai_parser
            for inp in claude_ai_parser.parse(path):
                await ingest_document(inp, ollama)
        else:
            log.warning("inbox_unknown_type", path=str(path))
            return

        _archive(path)
    except Exception as exc:
        log.error("inbox_handle_failed", path=str(path), error=str(exc))


def _archive(path: Path) -> None:
    dest_dir = path.parent / "_processed" / date.today().isoformat()
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest_dir / path.name))
