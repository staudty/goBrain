"""Gemma-powered summarization.

E4B (primary tier) writes a 200-word TL;DR + tags + the cleaned body.
Uses think:false for speed; the task is structured enough that thinking
mode just wastes tokens.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from .config import settings
from .ollama_client import OllamaClient

log = structlog.get_logger(__name__)

_SYSTEM = """You are a careful archivist summarizing an AI conversation into a permanent note.

Rules:
- The note will be searched later to remind the user of decisions and context. Bias toward preserving WHAT was decided and WHY.
- Output exactly three sections in this order, separated by a single blank line, nothing else:

SUMMARY: a single paragraph, 2-4 sentences, under 80 words. Lead with the outcome. No preamble.

TAGS: a comma-separated list of 3 to 8 lowercase tags (single words or short hyphenated phrases), most-specific first. No commentary.

KEY POINTS: 3 to 8 bullet points, each under 20 words. Cover: decisions made, code or files touched, open questions, next steps. No filler.

Do not add headings other than these three. Do not include the user's original text verbatim. Do not speculate about things that weren't discussed."""


@dataclass
class Summary:
    summary: str
    tags: list[str]
    key_points: list[str]


async def summarize_conversation(
    ollama: OllamaClient,
    conversation_text: str,
    *,
    max_input_tokens: int = 24_000,
) -> Summary:
    """Summarize a conversation. Truncates from the middle if too long,
    preserving head + tail which are usually the most informative."""
    truncated = _truncate_middle(conversation_text, max_input_tokens)

    raw = await ollama.chat(
        model=settings.model_primary,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": truncated},
        ],
        think=False,
        num_ctx=32_000,
        temperature=0.2,
    )
    return _parse(raw)


# --------------------------------------------------------------------------
def _truncate_middle(text: str, max_tokens: int) -> str:
    # Rough: assume 3.5 chars/token. Precise tokenization isn't worth it here —
    # the model's real context is generous and the summarizer tolerates slack.
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    keep_each = max_chars // 2 - 200
    head = text[:keep_each]
    tail = text[-keep_each:]
    return f"{head}\n\n<!-- [middle truncated: {len(text) - 2 * keep_each} chars] -->\n\n{tail}"


_SECTION_RE = re.compile(r"^(SUMMARY|TAGS|KEY POINTS)\s*:\s*", re.MULTILINE)


def _parse(raw: str) -> Summary:
    chunks: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(raw))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        chunks[m.group(1)] = raw[m.end():end].strip()

    summary = chunks.get("SUMMARY", "").strip()
    tags = [t.strip().lower() for t in chunks.get("TAGS", "").split(",") if t.strip()]
    key_points = [
        re.sub(r"^[-*•\d\.\)]\s*", "", line).strip()
        for line in chunks.get("KEY POINTS", "").splitlines()
        if line.strip()
    ]
    return Summary(summary=summary, tags=tags, key_points=key_points)
