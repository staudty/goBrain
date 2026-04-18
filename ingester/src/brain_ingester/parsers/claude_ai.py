"""Parse Claude.ai account data exports.

Placeholder for Phase 4 (Wed Apr 22). Anthropic's export is a ZIP with a
`conversations.json` at the root. Each conversation has a `uuid`, `name`,
`created_at`, and a `chat_messages` list. We finalize against a real export.
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..writers import IngestInput


def parse(path: Path) -> Iterator[IngestInput]:
    with zipfile.ZipFile(path) as zf:
        name = next(
            (n for n in zf.namelist() if n.endswith("conversations.json")),
            None,
        )
        if not name:
            return
        with zf.open(name) as f:
            data = json.load(f)

    for conv in data:
        messages = conv.get("chat_messages") or []
        if not messages:
            continue

        body_parts = []
        for m in messages:
            role = (m.get("sender") or "user").lower()
            text = m.get("text") or ""
            if not text and isinstance(m.get("content"), list):
                text = "\n".join(
                    b.get("text", "") for b in m["content"] if isinstance(b, dict)
                )
            label = "User" if role in ("user", "human") else "Claude"
            body_parts.append(f"### {label}\n\n{text}\n")

        yield IngestInput(
            source="claude-ai",
            source_id=str(conv.get("uuid") or conv.get("id")),
            conversation_text="\n".join(body_parts),
            started_at=_parse_ts(conv.get("created_at")),
            ended_at=_parse_ts(conv.get("updated_at")),
            project=conv.get("name") or None,
            model=conv.get("model"),
            turn_count=len(messages),
        )


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
