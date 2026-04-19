"""MCP server configuration."""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BRAIN_MCP_",
        extra="ignore",
    )

    postgres_dsn: str = "postgresql+psycopg://brain:CHANGEME@localhost:5432/brain"
    ollama_base_url: str = "http://127.0.0.1:11434"
    model_rerank: str = "gemma4:e2b"
    model_embed: str = "nomic-embed-text"

    # Retrieval tuning
    # 8 candidates keeps rerank wall-time tolerable even under heavy Ollama
    # contention (8 calls × ~1-2s warm = ~10-20s). 20 was fine when nothing
    # else competed for the GPU, but during bulk ingestion that's a lot more
    # model-swap thrashing. Override per-request via the `limit` param.
    search_candidates: int = 8
    search_return_default: int = 5       # post-rerank default return size
    max_chunks_per_document: int = 2     # diversity cap

    vault_path: Path = Path.home() / "Brain"


settings = Settings()
