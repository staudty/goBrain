"""Parse xAI / Grok data export files.

Placeholder for Phase 4 (Wed Apr 22). When Chris supplies a real export
we finalize the schema-matching logic.

xAI's export format (as of early 2026) appears to be JSON with a top-level
`conversations` array, each containing `id`, `title`, `created_at`, and
a `messages` array. We handle a few likely schema variants defensively.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..writers import IngestInput


def parse(path: Path) -> Iterator[IngestInput]:
    data = json.loads(path.read_text(encoding="utf-8"))
    conversations = data.get("conversations") or data.get("chats") or []

    for conv in conversations:
        messages = conv.get("messages") or conv.get("turns") or []
        if not messages:
            continue

        body_parts = []
        for m in messages:
            role = (m.get("role") or m.get("sender") or "user").lower()
            text = m.get("content") or m.get("text") or ""
            if isinstance(text, list):  # content blocks
                text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
            label = "User" if role in ("user", "human") else "Grok"
            body_parts.append(f"### {label}\n\n{text}\n")

        started = _parse_ts(conv.get("created_at") or conv.get("started_at"))
        ended = _parse_ts(conv.get("updated_at") or conv.get("ended_at"))

        yield IngestInput(
            source="grok",
            source_id=str(conv.get("id") or conv.get("conversation_id") or path.stem),
            conversation_text="\n".join(body_parts),
            started_at=started,
            ended_at=ended,
            project=conv.get("title") or None,
            model=conv.get("model"),
            turn_count=len(messages),
        )


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
