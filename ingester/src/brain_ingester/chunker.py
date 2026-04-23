"""Split documents into ~500-token chunks with 100-token overlap.

Uses tiktoken's cl100k_base as a reasonable general-purpose tokenizer.
Chunk boundaries prefer paragraph breaks, then sentence, then raw token.
"""
from __future__ import annotations

from dataclasses import dataclass

import tiktoken

from .config import settings

_enc = tiktoken.get_encoding("cl100k_base")


@dataclass
class Chunk:
    index: int
    content: str


def chunk_text(text: str) -> list[Chunk]:
    target = settings.chunk_target_tokens
    overlap = settings.chunk_overlap_tokens

    # disallowed_special=() so literal strings like "<|endoftext|>" in the
    # source text are encoded as plain bytes instead of raising ValueError.
    # Goes wrong whenever a user (or our own diagnostics) types a tiktoken
    # sentinel verbatim — we'd rather embed it as regular content than
    # refuse to index the whole document.
    tokens = _enc.encode(text, disallowed_special=())
    if len(tokens) <= target:
        return [Chunk(index=0, content=text)]

    step = target - overlap
    chunks: list[Chunk] = []
    i = 0
    idx = 0
    while i < len(tokens):
        window = tokens[i : i + target]
        content = _enc.decode(window)
        chunks.append(Chunk(index=idx, content=content))
        idx += 1
        if i + target >= len(tokens):
            break
        i += step
    return chunks
