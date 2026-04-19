# Pluto integration

Pluto = Chris's OpenClaw bot on the Mac Mini. It interacts with him via Telegram and performs tool work (email, calendar, web actions, etc.). To make Pluto's activity searchable in goBrain, three integrations need to happen. Two of them are Pluto-side code changes; one ships as part of goBrain and runs nightly.

## Big picture

```
Pluto (OpenClaw)
      │
      │  (A) on every tool_call / tool_result / error:
      │       POST http://127.0.0.1:8765/ingest/pluto-event
      ▼
  pluto_events table in Postgres (one row per event)
      │
      │  (B) at 03:05 every night, LaunchAgent calls:
      │       POST http://127.0.0.1:8765/admin/pluto/rollup
      ▼
  Gemma 4 E4B summarizes the day's events into markdown
      │
      ▼
  Vault note at sessions/pluto/daily-rollup/YYYY-MM-DD_daily-YYYY-MM-DD.md
  + Postgres row + embeddings (searchable via search_brain)

Pluto (OpenClaw) also watches a private Telegram "brain" channel:
      │
      │  (C) when Chris forwards a message to that channel:
      │       POST http://127.0.0.1:8765/ingest/document
      ▼
  Same ingestion pipeline as any drop — summary, chunks, vault note.
```

## A. Activity logger (Pluto-side code)

Every time Pluto invokes a tool OR receives a tool result OR errors, it should fire a small HTTP POST. The ingester endpoint is already live at `/ingest/pluto-event`. The schema:

```json
{
  "ts": "2026-04-19T14:03:12.123456+00:00",
  "kind": "tool_call",
  "tool_name": "send_email",
  "parent_session_id": "pluto-telegram-<chat-id>-<ts>",
  "payload": {
    "to": "someone@example.com",
    "subject": "re: something",
    "body": "..."
  }
}
```

Field semantics:
- **ts** — ISO-8601 UTC timestamp.
- **kind** — one of `tool_call` | `tool_result` | `message_in` | `message_out` | `error`.
- **tool_name** — only required for tool_call / tool_result; name of the tool as Pluto knows it.
- **parent_session_id** — a stable ID for the conversation / task that ties related events together. We recommend `pluto-telegram-<chat-id>-<start-timestamp>` — anything that groups events from the same user interaction.
- **payload** — free-form JSON with whatever's relevant for that event. Just don't put binary blobs in there; paths to files are better.

### Python hook pattern (drop into your OpenClaw code)

```python
import httpx
from datetime import datetime, timezone

_INGESTER = "http://127.0.0.1:8765"
_client = httpx.AsyncClient(timeout=5.0)

async def pluto_log_event(kind: str, *, tool_name: str | None = None,
                          parent_session_id: str | None = None,
                          payload: dict | None = None) -> None:
    """Fire-and-forget: record an event to goBrain. Errors are swallowed so
    a transient brain outage never breaks Pluto's main loop."""
    try:
        await _client.post(f"{_INGESTER}/ingest/pluto-event", json={
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "tool_name": tool_name,
            "parent_session_id": parent_session_id,
            "payload": payload,
        })
    except Exception:
        pass
```

### Where to call it

Wrap every tool invocation:

```python
await pluto_log_event("tool_call", tool_name="send_email",
                     parent_session_id=session_id,
                     payload={"to": recipient, "subject": subject})

result = await send_email(...)

await pluto_log_event("tool_result", tool_name="send_email",
                     parent_session_id=session_id,
                     payload={"status": result.status, "message_id": result.id})
```

And for inbound / outbound user messages:

```python
await pluto_log_event("message_in",
                     parent_session_id=session_id,
                     payload={"text": msg.text, "from": msg.from_user.username})
```

### Testing the hook without touching Pluto code

You can validate the ingester is accepting events with plain curl:

```bash
curl -s -X POST http://127.0.0.1:8765/ingest/pluto-event \
  -H "Content-Type: application/json" \
  -d '{
    "ts": "2026-04-19T14:00:00+00:00",
    "kind": "tool_call",
    "tool_name": "test_tool",
    "parent_session_id": "manual-test-1",
    "payload": {"note": "curl test"}
  }'
# {"ok":true}
```

Then verify it landed:

```bash
docker compose exec postgres psql -U brain -d brain -c \
  "SELECT ts, kind, tool_name FROM pluto_events ORDER BY ts DESC LIMIT 5;"
```

## B. Nightly rollup (goBrain-side — already built)

Installed by running `./setup-pluto-rollup.sh` on the Mac Mini. This registers a LaunchAgent that runs `curl -X POST http://127.0.0.1:8765/admin/pluto/rollup` at 03:05 local time every night.

What happens:
1. Endpoint determines target date (default: yesterday UTC).
2. Queries all `pluto_events` rows in that UTC day.
3. If 0 events, returns immediately with a note.
4. Otherwise serializes events as JSON, hands to Gemma 4 E4B with a summarization system prompt (see `pluto_rollup.py`).
5. Wraps the Gemma output in a markdown note, ingests via the normal pipeline.
6. Result: a searchable `sessions/pluto/daily-rollup/YYYY-MM-DD_daily-YYYY-MM-DD.md` note, plus Postgres row + embeddings.

### Manual trigger

Useful for backfill or testing:

```bash
# Yesterday (default)
curl -X POST http://127.0.0.1:8765/admin/pluto/rollup

# Specific date
curl -X POST "http://127.0.0.1:8765/admin/pluto/rollup?target_date=2026-04-18"
```

### Checking it ran

```bash
# LaunchAgent logs
tail -30 ~/Library/Logs/gobag-pluto-rollup.out.log

# The generated note
ls ~/Brain/sessions/pluto/daily-rollup/ | tail
```

## C. Telegram forward-to-brain (Pluto-side code)

For catching content from sources we can't auto-ingest (Grok mobile app, random articles, screenshots with OCR'd text, notes dictated in the moment), Pluto can watch a dedicated Telegram channel.

Suggested setup:
1. Create a private Telegram channel (just you), named something like `brain-inbox`.
2. Give Pluto membership / admin access.
3. When a message arrives in that channel, Pluto grabs the text and POSTs to `/ingest/document`:

```python
async def handle_brain_channel_message(msg) -> None:
    text = msg.text or msg.caption or ""
    if not text.strip():
        return

    # Use the message's unique ID so re-forwards get deduplicated
    source_id = f"telegram-{msg.chat.id}-{msg.message_id}"

    await _client.post(f"{_INGESTER}/ingest/document", json={
        "source": "telegram-brain",
        "source_id": source_id,
        "conversation_text": text,
        "started_at": msg.date.isoformat(),
        "project": "brain-inbox",
        "turn_count": 1,
    })
```

Forwarded conversations from Grok or ChatGPT retain their structure and get summarized + embedded just like any other document.

## Recap — what you do next

1. **Install the rollup schedule** (takes effect immediately, but won't produce output until Pluto starts logging events):
   ```bash
   cd ~/goBrain/mac-mini && ./setup-pluto-rollup.sh
   ```

2. **Add the `pluto_log_event()` helper to OpenClaw** and wire it into every tool invocation + user message.

3. **(Optional) Create the Telegram `brain-inbox` channel** and add the forward handler to Pluto.

4. **Verify end-to-end**: POST a fake event via curl, run the rollup manually for today's date, confirm a note appears in `~/Brain/sessions/pluto/`.
