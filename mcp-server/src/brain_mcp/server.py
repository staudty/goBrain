"""MCP stdio server exposing brain retrieval tools to Claude Code / Desktop.

Run via:  uv run brain-mcp
Register in Claude Code:  claude mcp add brain 'uv run brain-mcp' --scope user
"""
from __future__ import annotations

import asyncio
import json
import logging

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import retrieval

logging.basicConfig(level=logging.INFO)
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger(__name__)

app = Server("goBrain")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_brain",
            description=(
                "Search the unified second-brain across all AI conversations and notes. "
                "Returns the most relevant ~500-token chunks, already re-ranked by a local "
                "model. Use this when the user references earlier discussions, prior decisions, "
                "or anything you don't have in current context."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language search query."},
                    "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 15,
                              "description": "How many chunks to return."},
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by source: claude-code, openclaw, claude-desktop, claude-ai, grok, inbox."
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="recent_sessions",
            description="List the most recently ingested conversations with their summaries.",
            inputSchema={
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                    "source": {"type": "string", "description": "Optional source filter."},
                },
            },
        ),
        Tool(
            name="get_document",
            description=(
                "Fetch the full markdown of a specific vault document by its vault_path. "
                "Use only when search_brain returns a hit whose full content is needed."
            ),
            inputSchema={
                "type": "object",
                "properties": {"vault_path": {"type": "string"}},
                "required": ["vault_path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "search_brain":
            hits = await retrieval.search(
                query=arguments["query"],
                limit=int(arguments.get("limit", 5)),
                sources=arguments.get("sources"),
            )
            payload = [
                {
                    "score": round(h.score, 3),
                    "source": h.source,
                    "vault_path": h.vault_path,
                    "project": h.project,
                    "started_at": h.started_at,
                    "summary": h.summary,
                    "content": h.content,
                }
                for h in hits
            ]
        elif name == "recent_sessions":
            payload = retrieval.recent_documents(
                n=int(arguments.get("n", 10)),
                source=arguments.get("source"),
            )
        elif name == "get_document":
            payload = {"content": retrieval.get_document_text(arguments["vault_path"])}
        else:
            raise ValueError(f"unknown tool: {name}")

        return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]
    except Exception as exc:
        log.exception("tool_failed", tool=name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


async def _main() -> None:
    log.info("brain_mcp_start")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()
