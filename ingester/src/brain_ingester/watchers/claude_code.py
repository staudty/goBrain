"""Watch Claude Code session JSONL files and ingest finished sessions.

Claude Code writes per-project, per-session JSONL logs under
`~/.claude/projects/<project>/<session-id>.jsonl`. Each line is a JSON event
describing a user message, assistant message, tool use, or tool result.

We consider a session "finished" when no new lines appear for `idle_secs`
(default 5 minutes). On finish, we reassemble the conversation into
human-readable markdown and hand it to the writer.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog
from watchfiles import Change, awatch

from ..config import settings
from ..ollama_client import OllamaClient
from ..writers import IngestInput, ingest_document

log = structlog.get_logger(__name__)

IDLE_SECS = 300


@dataclass
class SessionState:
    path: Path
    project: str
    session_id: str
    last_line_at: float = field(default_factory=time.monotonic)
    first_seen_at: float = field(default_factory=time.monotonic)


async def run(ollama: OllamaClient, stop: asyncio.Event, root: Path | None = None) -> None:
    if root is None:
        root = settings.claude_code_projects_dir
    # Allow the directory to appear later (e.g., when a sync task first populates it)
    if not root.exists():
        log.info("claude_code_dir_waiting", path=str(root))
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log.warning("claude_code_dir_mkdir_failed", path=str(root), error=str(exc))
            return

    log.info("claude_code_watcher_start", root=str(root))
    sessions: dict[Path, SessionState] = {}

    async def sweep_idle() -> None:
        while not stop.is_set():
            await asyncio.sleep(30)
            now = time.monotonic()
            to_finish = [
                s for s in list(sessions.values())
                if now - s.last_line_at > IDLE_SECS
            ]
            for s in to_finish:
                try:
                    await _ingest(s, ollama)
                except Exception as exc:
                    log.error("ingest_failed", path=str(s.path), error=str(exc))
                sessions.pop(s.path, None)

    sweep_task = asyncio.create_task(sweep_idle())

    try:
        async for changes in awatch(root, stop_event=stop):
            for change, path_str in changes:
                path = Path(path_str)
                if not path.name.endswith(".jsonl"):
                    continue
                if change is Change.deleted:
                    sessions.pop(path, None)
                    continue
                state = sessions.get(path) or _new_state(path, root)
                state.last_line_at = time.monotonic()
                sessions[path] = state
    finally:
        sweep_task.cancel()
        # Flush remaining sessions on shutdown
        for s in sessions.values():
            try:
                await _ingest(s, ollama)
            except Exception as exc:
                log.error("flush_failed", path=str(s.path), error=str(exc))


def _new_state(path: Path, root: Path) -> SessionState:
    rel = path.relative_to(root)
    parts = rel.parts
    project = parts[0] if len(parts) > 1 else "unknown"
    session_id = path.stem
    return SessionState(path=path, project=project, session_id=session_id)


async def _ingest(state: SessionState, ollama: OllamaClient) -> None:
    """Parse JSONL → markdown conversation → ingest."""
    if not state.path.exists() or state.path.stat().st_size == 0:
        return

    lines = state.path.read_text(encoding="utf-8", errors="replace").splitlines()
    events: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not events:
        return

    body, turn_count, tool_count, started_at, ended_at, model = _render_conversation(events)

    await ingest_document(
        IngestInput(
            source="claude-code",
            source_id=f"{state.project}/{state.session_id}",
            conversation_text=body,
            started_at=started_at,
            ended_at=ended_at,
            project=state.project,
            model=model,
            turn_count=turn_count,
            tool_call_count=tool_count,
        ),
        ollama,
    )
    log.info("claude_code_ingested",
             project=state.project, session=state.session_id,
             turns=turn_count, tools=tool_count)


def _render_conversation(events: list[dict]) -> tuple[str, int, int, datetime | None, datetime | None, str | None]:
    """Reduce a JSONL event stream to a human-readable markdown transcript.

    Claude Code's schema evolves; this handler is defensive and tolerates
    unknown event types (they are rendered as a short marker).
    """
    lines: list[str] = []
    turn_count = 0
    tool_count = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    model: str | None = None

    for ev in events:
        ts = _parse_ts(ev.get("timestamp") or ev.get("ts"))
        if ts:
            started_at = started_at or ts
            ended_at = ts

        etype = ev.get("type") or ev.get("role") or ""
        model = model or ev.get("model")

        if etype in ("user", "human"):
            turn_count += 1
            lines.append(f"### User\n\n{_extract_text(ev)}\n")
        elif etype in ("assistant", "ai"):
            turn_count += 1
            lines.append(f"### Assistant\n\n{_extract_text(ev)}\n")
        elif etype in ("tool_use", "tool_call"):
            tool_count += 1
            name = ev.get("name") or ev.get("tool_name") or "tool"
            payload = ev.get("input") or ev.get("arguments") or {}
            lines.append(f"**Tool call: `{name}`**\n\n```json\n{json.dumps(payload, indent=2)[:2000]}\n```\n")
        elif etype in ("tool_result",):
            result = ev.get("content") or ev.get("result") or ""
            snippet = (result if isinstance(result, str) else json.dumps(result))[:1500]
            lines.append(f"**Tool result**\n\n```\n{snippet}\n```\n")
        else:
            continue  # quietly drop unknown event types

    return "\n".join(lines), turn_count, tool_count, started_at, ended_at, model


def _extract_text(ev: dict) -> str:
    content = ev.get("content") or ev.get("text") or ""
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
        return "\n".join(p for p in parts if p)
    if isinstance(content, str):
        return content
    return json.dumps(content)


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
