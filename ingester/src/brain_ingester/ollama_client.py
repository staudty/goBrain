"""Thin Ollama client — chat (with think:false) and embeddings."""
from __future__ import annotations

import httpx
import structlog

from .config import settings

log = structlog.get_logger(__name__)


class OllamaClient:
    def __init__(self, base_url: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or settings.ollama_base_url,
            timeout=httpx.Timeout(120.0, read=120.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        think: bool = False,
        num_ctx: int = 8192,
        temperature: float = 0.3,
    ) -> str:
        """Single-turn chat. Always non-streaming; returns the assistant content."""
        r = await self._client.post(
            "/api/chat",
            json={
                "model": model,
                "messages": messages,
                "think": think,          # critical: disable thinking for fast tasks
                "stream": False,
                "options": {"num_ctx": num_ctx, "temperature": temperature},
            },
        )
        r.raise_for_status()
        data = r.json()
        return data["message"]["content"]

    async def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        """Batch-embed. Ollama's /api/embed accepts a list."""
        r = await self._client.post(
            "/api/embed",
            json={"model": model, "input": inputs},
        )
        r.raise_for_status()
        return r.json()["embeddings"]
