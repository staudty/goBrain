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
    """Turn a JSONL path into (project, session_id). Handles both flat and
    nested sub-agent layouts.

    Flat (normal session):
        <root>/<project>/<session-uuid>.jsonl
        -> project=<project>, session_id=<session-uuid>

    Nested (sub-agent trace spawned by the Task tool inside a parent session):
        <root>/<project>/<parent-session-uuid>/subagents/agent-<id>.jsonl
        -> project=<project>, session_id=<parent-session-uuid>/subagents/<agent-id>

    Keeping the parent UUID in session_id preserves traceability (you can
    search and see which parent session ran each sub-agent) AND prevents
    dedup collisions between same-named sub-agents across different parents.
    """
    rel = path.relative_to(root)
    parts = rel.parts
    project = parts[0] if len(parts) > 1 else "unknown"

    # Sub-agent: <project>/<parent-uuid>/subagents/agent-<id>.jsonl
    if len(parts) >= 4 and parts[-2] == "subagents":
        parent_session_uuid = parts[-3]
        agent_id = path.stem
        session_id = f"{parent_session_uuid}/subagents/{agent_id}"
    else:
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

    source, project = _classify(events, state.project)

    await ingest_document(
        IngestInput(
            source=source,
            source_id=f"{state.project}/{state.session_id}",
            conversation_text=body,
            started_at=started_at,
            ended_at=ended_at,
            project=project,
            model=model,
            turn_count=turn_count,
            tool_call_count=tool_count,
        ),
        ollama,
    )
    log.info("claude_code_ingested",
             source=source, project=project, session=state.session_id,
             turns=turn_count, tools=tool_count)


def _classify(events: list[dict], fallback_project: str) -> tuple[str, str]:
    """Decide whether this session is plain Claude Code or an OpenClaw
    (agent-driven) session, and produce a clean project name.

    OpenClaw sessions are Claude Code sessions whose cwd lives under one
    of `settings.openclaw_cwd_subpaths` (default: ~/clawd). Their project
    name is derived from the cwd tail rather than the Claude-Code-encoded
    directory name (e.g. cwd=/Users/<you>/clawd/puck-engine → project=
    "puck-engine"; bare cwd=/Users/<you>/clawd → project="clawd").
    """
    from pathlib import Path as _P
    home = _P.home()
    openclaw_prefixes = [
        str((home / sub).resolve()) for sub in settings.openclaw_cwd_subpaths
    ]

    cwd = next((ev.get("cwd") for ev in events if ev.get("cwd")), None)
    if not cwd:
        return "claude-code", fallback_project

    try:
        cwd_resolved = str(_P(cwd).resolve())
    except (OSError, RuntimeError):
        cwd_resolved = cwd

    for prefix in openclaw_prefixes:
        if cwd_resolved == prefix or cwd_resolved.startswith(prefix + "/"):
            try:
                rel = _P(cwd_resolved).relative_to(prefix)
                rel_str = str(rel).strip("./")
            except ValueError:
                rel_str = ""
            project = rel_str.replace("/", "_") if rel_str else _P(prefix).name
            return "openclaw", project

    return "claude-code", fallback_project


def _render_conversation(events: list[dict]) -> tuple[str, int, int, datetime | None, datetime | None, str | None]:
    """Reduce a JSONL event stream to a human-readable markdown transcript.

    Claude Code's JSONL has two top-level event kinds we render:
      type=user       — either a user prompt or a tool result (has toolUseResult)
      type=assistant  — one or more content blocks (text + tool_use)
    Everything else (queue-operation, summary pings, etc.) is skipped.

    Content lives at ev["message"]["content"]:
      - user: plain string (or list for tool results)
      - assistant: list of blocks like {type:"text", text:"..."} or
                   {type:"tool_use", name, input}.
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

        etype = ev.get("type") or ""
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        model = model or msg.get("model") or ev.get("model")

        if etype == "user":
            # A user event is either a fresh human prompt or a tool-result
            # delivery. Tool results carry a toolUseResult sibling field and
            # their message.content is a list of tool_result blocks.
            if ev.get("toolUseResult") is not None:
                rendered = _render_user_tool_result(msg, ev)
                if rendered:
                    lines.append(rendered)
            else:
                text = _extract_user_text(msg)
                if text:
                    turn_count += 1
                    lines.append(f"### User\n\n{text}\n")
        elif etype == "assistant":
            blocks_md, tools_seen = _render_assistant_blocks(msg)
            if blocks_md:
                turn_count += 1
                tool_count += tools_seen
                lines.append(f"### Assistant\n\n{blocks_md}\n")
        else:
            continue  # queue-operation and friends — skip

    return "\n".join(lines), turn_count, tool_count, started_at, ended_at, model


def _extract_user_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        # Some user events use a block list; pluck any text blocks.
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(p for p in parts if p).strip()
    return ""


def _render_assistant_blocks(msg: dict) -> tuple[str, int]:
    """Return (markdown, tool_call_count) for the assistant's content blocks."""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip(), 0
    if not isinstance(content, list):
        return "", 0

    parts: list[str] = []
    tools = 0
    for c in content:
        if not isinstance(c, dict):
            continue
        btype = c.get("type")
        if btype == "text":
            text = (c.get("text") or "").strip()
            if text:
                parts.append(text)
        elif btype == "tool_use":
            tools += 1
            name = c.get("name") or "tool"
            payload = c.get("input") or {}
            payload_str = json.dumps(payload, indent=2)[:2000]
            parts.append(f"**Tool call: `{name}`**\n\n```json\n{payload_str}\n```")
        elif btype == "thinking":
            # Skip — thinking blocks are internal chain-of-thought we don't
            # want in the vault.
            continue
    return "\n\n".join(parts), tools


def _render_user_tool_result(msg: dict, ev: dict) -> str:
    """Render a tool_result event. content is usually a list of blocks
    like {type:"tool_result", tool_use_id, content:"..."}. Cap size."""
    content = msg.get("content")
    snippets: list[str] = []
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            inner = c.get("content")
            if isinstance(inner, str):
                snippets.append(inner)
            elif isinstance(inner, list):
                for sub in inner:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        snippets.append(sub.get("text", ""))
    elif isinstance(content, str):
        snippets.append(content)
    else:
        # Fall back to the sibling toolUseResult field
        tur = ev.get("toolUseResult")
        if isinstance(tur, str):
            snippets.append(tur)
        elif tur is not None:
            snippets.append(json.dumps(tur)[:1500])

    body = "\n".join(s for s in snippets if s).strip()
    if not body:
        return ""
    return f"**Tool result**\n\n```\n{body[:1500]}\n```\n"


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
