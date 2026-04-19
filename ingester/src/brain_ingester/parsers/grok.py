"""Parse xAI / Grok data export files.

Grok's export is a ZIP containing `ttl/30d/export_data/<user-uuid>/prod-grok-backend.json`
(plus asset files we currently ignore). The JSON has shape:

    {
      "conversations": [
        {
          "conversation": { "id": "...", "title": "...", "create_time": "ISO", "modify_time": "ISO", ... },
          "responses": [
            {
              "response": {
                "_id": "...",
                "message": "<text>",
                "sender": "human" | "ASSISTANT",
                "create_time": {"$date": {"$numberLong": "<millis-since-epoch>"}},
                "model": "grok-4",
                "parent_response_id": "...",
                ...
              }
            },
            ...
          ]
        },
        ...
      ]
    }

Timestamps on conversations are ISO-8601 strings; timestamps on individual responses
are MongoDB extended-JSON objects. We handle both.
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..writers import IngestInput


def parse(path: Path) -> Iterator[IngestInput]:
    data = _load_backend_json(path)
    if data is None:
        return

    for wrapper in data.get("conversations", []):
        conv = wrapper.get("conversation") or {}
        responses = wrapper.get("responses") or []
        if not responses:
            continue

        body_parts: list[str] = []
        last_model: str | None = None
        for r_wrap in responses:
            r = r_wrap.get("response") or {}
            sender = (r.get("sender") or "").lower()
            text = r.get("message", "") or ""
            label = "User" if sender in ("human", "user") else "Grok"
            body_parts.append(f"### {label}\n\n{text}\n")
            if r.get("model"):
                last_model = r["model"]

        conv_id = str(conv.get("id") or conv.get("_id") or "unknown")

        yield IngestInput(
            source="grok",
            source_id=conv_id,
            conversation_text="\n".join(body_parts),
            started_at=_parse_ts(conv.get("create_time")),
            ended_at=_parse_ts(conv.get("modify_time")),
            project=conv.get("title") or None,
            model=last_model,
            turn_count=len(responses),
        )


def _load_backend_json(path: Path):
    """Return the parsed prod-grok-backend.json from either a ZIP or a bare JSON file."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            name = next(
                (n for n in zf.namelist() if n.endswith("prod-grok-backend.json")),
                None,
            )
            if not name:
                return None
            with zf.open(name) as f:
                return json.load(f)
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_ts(value) -> datetime | None:
    """Handle Grok's two timestamp formats: ISO-8601 string or MongoDB extended JSON."""
    if not value:
        return None
    if isinstance(value, dict):
        inner = value.get("$date")
        if isinstance(inner, dict):
            nl = inner.get("$numberLong")
            if nl is not None:
                return datetime.fromtimestamp(int(nl) / 1000, tz=timezone.utc)
        if isinstance(inner, str):
            return _parse_iso(inner)
        return None
    if isinstance(value, (int, float)):
        # epoch seconds vs millis — anything > 10^12 is millis
        if value > 10**12:
            value = value / 1000
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        return _parse_iso(value)
    return None


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
