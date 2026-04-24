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

    # Remote HTTP transport — only used by `brain-mcp-http` (Claude iOS and
    # other remote MCP clients). The stdio transport (`brain-mcp`) doesn't
    # touch these.
    http_host: str = "127.0.0.1"         # bind-host; keep loopback behind TLS proxy
    http_port: int = 8766

    # Static dev-bypass token for local curl testing. Optional, but if set
    # it grants full access — treat as admin credential. For real remote
    # clients (Claude iOS, etc.) use the OAuth credentials below instead.
    remote_bearer_token: str | None = None

    # OAuth 2.0 client_credentials grant, per MCP spec. Anthropic's Custom
    # Connector UI asks for these. Generate with `openssl rand -hex 32` for
    # each. Paste the pair into claude.ai's OAuth Client ID + Secret fields.
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_token_ttl_seconds: int = 3600  # 1h, refreshed automatically by clients


settings = Settings()
