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

    postgres_dsn: str = "postgresql+psycopg://brain:CHANGEME@192.168.1.178:5433/brain"
    ollama_base_url: str = "http://127.0.0.1:11434"
    model_rerank: str = "gemma4:e2b"
    model_embed: str = "nomic-embed-text"

    # Retrieval tuning
    search_candidates: int = 20          # pgvector ANN top-K pulled before re-rank
    search_return_default: int = 5       # post-rerank default return size
    max_chunks_per_document: int = 2     # diversity cap

    vault_path: Path = Path.home() / "Brain"


settings = Settings()
