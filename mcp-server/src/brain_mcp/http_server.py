"""Remote MCP endpoint — Streamable HTTP transport with bearer-token auth.

Run via: `uv run brain-mcp-http`. Binds to ${BRAIN_MCP_HTTP_HOST}:${BRAIN_MCP_HTTP_PORT}
(defaults 127.0.0.1:8766). Intended to sit behind a TLS reverse proxy
(Caddy, Cloudflare Tunnel, etc.) that exposes it at an https:// URL.

The server reuses the same tool registry as the stdio variant (server.py) —
one source of truth, two transports.

Auth: set BRAIN_MCP_REMOTE_BEARER_TOKEN in .env. Any request without a
matching `Authorization: Bearer <token>` header gets 401. Token comparison
uses secrets.compare_digest. If the env var is empty the server refuses
to start (refuses to bind publicly without auth).
"""
from __future__ import annotations

import contextlib
import logging
import secrets

import structlog
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount

from .config import settings
from .server import app as mcp_app

logging.basicConfig(level=logging.INFO)
log = structlog.get_logger(__name__)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request whose Authorization header doesn't match the
    configured bearer token. Skips /health so reverse-proxies can probe."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        provided = header.split(" ", 1)[1].strip()
        if not secrets.compare_digest(provided, self._token):
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return await call_next(request)


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _build_app() -> Starlette:
    token = settings.remote_bearer_token
    if not token:
        raise RuntimeError(
            "BRAIN_MCP_REMOTE_BEARER_TOKEN is empty. Refusing to start an "
            "unauthenticated remote MCP server. Generate one with "
            "`openssl rand -hex 32` and set it in .env."
        )

    # Stateless=True keeps each request self-contained (no server-side
    # session state to lose on restart). Fine for our small, read-only
    # tool set; revisit if we add long-running streams.
    session_manager = StreamableHTTPSessionManager(app=mcp_app, stateless=True)

    async def mcp_endpoint(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_starlette):
        async with session_manager.run():
            log.info("brain_mcp_http_start",
                     host=settings.http_host,
                     port=settings.http_port)
            yield
            log.info("brain_mcp_http_stop")

    from starlette.routing import Route

    starlette_app = Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Mount("/mcp", app=mcp_endpoint),
        ],
        middleware=[Middleware(BearerAuthMiddleware, token=token)],
        lifespan=lifespan,
    )
    # Streamable HTTP clients POST to exactly /mcp. Default Starlette
    # routing 307-redirects that to /mcp/, which curl -X POST won't
    # follow and Claude iOS won't honor either. Turn it off on the
    # router directly (the Starlette constructor didn't accept this
    # kwarg until ~0.38).
    starlette_app.router.redirect_slashes = False
    return starlette_app


def run() -> None:
    uvicorn.run(
        _build_app(),
        host=settings.http_host,
        port=settings.http_port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
