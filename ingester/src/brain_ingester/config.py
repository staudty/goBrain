"""Runtime configuration loaded from env + .env."""
from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BRAIN_",
        extra="ignore",
    )

    # --- HTTP -----------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8765

    # --- Vault ---------------------------------------------------------------
    vault_path: Path = Path.home() / "Brain"

    # --- Storage: Postgres (primary) -----------------------------------------
    # Before the NAS RAM upgrade lands, leave postgres_dsn empty and the
    # ingester will buffer to sqlite at fallback_sqlite_path. Once Postgres is
    # up, set this and the ingester drains the buffer.
    postgres_dsn: str | None = None
    fallback_sqlite_path: Path = Path.home() / ".goBrain" / "buffer.sqlite"

    # --- Ollama --------------------------------------------------------------
    ollama_base_url: str = "http://127.0.0.1:11434"
    model_fast: str = "gemma4:e2b"          # classification / routing
    model_primary: str = "gemma4:e4b"       # summarization / re-ranking
    model_embed: str = "nomic-embed-text"
    embed_dim: int = 768

    # --- llama.cpp (heavy tier) ----------------------------------------------
    llamacpp_base_url: str = "http://127.0.0.1:8081"
    llamacpp_launchd_label: str = "com.gobag.llamacpp"

    # --- Watcher toggles -----------------------------------------------------
    watch_claude_code: bool = True

    # Primary Claude Code directory (this machine's own sessions).
    claude_code_projects_dir: Path = Path.home() / ".claude" / "projects"

    # Additional Claude Code directories to watch (e.g., sessions shipped from
    # a Windows PC into a synced Brain subfolder). Comma-separated paths in env.
    # Each directory should mirror the same structure as ~/.claude/projects —
    # <project-name>/<session-uuid>.jsonl.
    claude_code_extra_dirs: list[Path] = [
        Path.home() / "Brain" / ".claude-code-sources" / "pc",
    ]

    watch_claude_desktop: bool = True
    watch_inbox: bool = True
    inbox_path: Path = Path.home() / "Brain" / "_inbox"

    # --- Chunking ------------------------------------------------------------
    chunk_target_tokens: int = 500
    chunk_overlap_tokens: int = 100

    # --- Source-specific filters --------------------------------------------
    # Grok "Companion" chats (roleplay personas like Ani and Mika) should be
    # skipped during ingestion. Title pattern match: "Chat with <name>" or
    # "Greeting <name>, ...". Override via BRAIN_GROK_COMPANION_NAMES (comma
    # or JSON list).
    grok_companion_names: list[str] = ["Ani", "Mika", "Valentine", "Rudi"]

    # Claude Code sessions whose cwd starts with one of these paths (relative
    # to $HOME) get tagged source=openclaw instead of source=claude-code.
    # Useful for distinguishing interactive Claude Code sessions from
    # autonomous agent activity driven by OpenClaw (or similar wrappers)
    # that happens to run on top of the same Claude Code JSONL schema.
    # Default ~/clawd matches the standard OpenClaw install directory.
    openclaw_cwd_subpaths: list[str] = ["clawd"]


settings = Settings()
