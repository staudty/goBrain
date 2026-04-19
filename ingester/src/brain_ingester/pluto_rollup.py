"""Daily rollup of Pluto's tool-call activity into a searchable vault note.

Run on a schedule (via LaunchAgent) or on-demand via the /admin/pluto/rollup
endpoint. Pulls every pluto_events row for a target date, hands the serialized
event stream to Gemma E4B for summarization, and writes the result as a
regular ingested document (so it shows up in search_brain and in the vault).
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

import structlog
from sqlalchemy import text

from .db import pg_session, postgres_available
from .ollama_client import OllamaClient
from .writers import IngestInput, ingest_document

log = structlog.get_logger(__name__)


_ROLLUP_SYSTEM = """You are a careful assistant producing a daily digest of one AI agent's (nicknamed Pluto) actions.

You will receive a JSON array of events recorded across one day. Each event has:
  - ts:      ISO timestamp
  - kind:    tool_call | tool_result | message_in | message_out | error
  - tool_name: optional, for tool_call/result
  - payload: a JSON object with task-specific fields

Produce a single markdown document with these sections in order, nothing else:

SUMMARY: 2-5 sentences, plain English, covering what Pluto did today overall. Lead with notable / important actions; suppress low-signal chatter. No preamble.

TAGS: 5-12 lowercase tags describing the day — people Pluto interacted with, systems it touched, topics of work, any errors of note. Comma-separated, single line.

TIMELINE: a chronological bulleted list. Each bullet is one line starting with HH:MM-HH:MM if events group together, then a terse description of what happened. Group consecutive events that were clearly part of one task into a single bullet; don't dump every low-level event.

ERRORS: any error events or failed tool calls, one per line. If none, write "None." and move on.

Be terse. A good rollup is readable in under 60 seconds and lets a human quickly skim what Pluto was up to."""


def _day_bounds_utc(target: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def _fetch_events(target: date) -> list[dict]:
    start, end = _day_bounds_utc(target)
    sql = text("""
        SELECT ts, kind, tool_name, parent_session_id, payload
        FROM pluto_events
        WHERE ts >= :start AND ts < :end
        ORDER BY ts ASC
    """)
    with pg_session() as session:
        rows = session.execute(sql, {"start": start, "end": end}).mappings().all()
    return [
        {
            "ts": r["ts"].isoformat(),
            "kind": r["kind"],
            "tool_name": r["tool_name"],
            "parent_session_id": r["parent_session_id"],
            "payload": r["payload"],
        }
        for r in rows
    ]


def _truncate_events_for_prompt(events: list[dict], max_chars: int = 40_000) -> str:
    """Serialize events as compact JSON, truncating oldest if too long."""
    full = json.dumps(events, indent=1, default=str)
    if len(full) <= max_chars:
        return full
    # Keep the most recent events preferentially
    keep: list[dict] = []
    running = 0
    for ev in reversed(events):
        chunk = json.dumps(ev, default=str)
        if running + len(chunk) > max_chars - 200:
            break
        keep.append(ev)
        running += len(chunk) + 2
    keep.reverse()
    head = {
        "_truncation_note": f"dropped {len(events) - len(keep)} earlier events to fit prompt budget",
    }
    return json.dumps([head, *keep], indent=1, default=str)


async def run_rollup(target: date | None = None, ollama: OllamaClient | None = None) -> dict:
    """Produce and ingest a daily rollup. Returns stats.

    target: the date to roll up (UTC). Defaults to yesterday.
    """
    if target is None:
        target = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    if not postgres_available():
        return {"ok": False, "error": "postgres not configured"}

    own_ollama = ollama is None
    if own_ollama:
        ollama = OllamaClient()

    try:
        events = _fetch_events(target)
        log.info("pluto_rollup_start", date=target.isoformat(), events=len(events))

        if not events:
            log.info("pluto_rollup_no_events", date=target.isoformat())
            return {"ok": True, "date": target.isoformat(), "events": 0, "note": "no events"}

        prompt = _truncate_events_for_prompt(events)
        summary_md = await ollama.chat(
            model="gemma4:e4b",
            messages=[
                {"role": "system", "content": _ROLLUP_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            think=False,
            num_ctx=32_000,
            temperature=0.2,
        )

        body = f"# Pluto daily rollup — {target.isoformat()}\n\n" \
               f"_{len(events)} events across the day._\n\n" \
               f"{summary_md.strip()}\n"

        vault_path = await ingest_document(
            IngestInput(
                source="pluto",
                source_id=f"daily-{target.isoformat()}",
                conversation_text=body,
                started_at=_day_bounds_utc(target)[0],
                ended_at=_day_bounds_utc(target)[1],
                project="daily-rollup",
                model="gemma4:e4b",
                turn_count=len(events),
            ),
            ollama,
        )

        log.info("pluto_rollup_done", date=target.isoformat(),
                 events=len(events), vault_path=str(vault_path))
        return {"ok": True, "date": target.isoformat(), "events": len(events),
                "vault_path": str(vault_path)}

    finally:
        if own_ollama:
            await ollama.aclose()
