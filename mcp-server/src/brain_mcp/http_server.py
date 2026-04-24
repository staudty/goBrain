"""Remote MCP endpoint — Streamable HTTP transport with OAuth 2.0 auth.

Run via: `uv run brain-mcp-http`. Binds to ${BRAIN_MCP_HTTP_HOST}:${BRAIN_MCP_HTTP_PORT}
(defaults 127.0.0.1:8766). Intended to sit behind a TLS reverse proxy
(Cloudflare Tunnel, Caddy, etc.) that exposes it at an https:// URL.

The server reuses the same tool registry as the stdio variant (server.py) —
one source of truth, two transports.

Auth, per MCP spec (2025-06-18+):
  - Protects /mcp with Bearer tokens issued by this same origin's OAuth
    authorization server.
  - Serves OAuth discovery metadata at:
        /.well-known/oauth-authorization-server (RFC 8414)
        /.well-known/oauth-protected-resource   (RFC 9728, referenced by
                                                 WWW-Authenticate on 401s)
  - Issues access tokens at POST /token via the client_credentials grant,
    authenticated by a pre-shared (client_id, client_secret) pair from .env.
    Supports both client_secret_post (form body) and client_secret_basic
    (HTTP Basic) auth methods.

Anthropic's Custom Connector UI asks for an OAuth Client ID + Client Secret.
Paste the values from .env into those fields on claude.ai and Claude takes
care of the token dance per the MCP spec on every invocation.

For local curl testing there is an optional BRAIN_MCP_REMOTE_BEARER_TOKEN
static token that bypasses OAuth. Treat it as an admin credential; prefer
OAuth for anything long-lived.
"""
from __future__ import annotations

import base64
import contextlib
import logging
import secrets
import time
from typing import Iterable

import structlog
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .config import settings
from .server import app as mcp_app

logging.basicConfig(level=logging.INFO)
log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# In-memory access-token store
#
# A restart invalidates all outstanding tokens; MCP clients (Claude iOS,
# etc.) simply re-run client_credentials on the next call and carry on. No
# user session is lost because the tokens were machine-issued anyway.
# ---------------------------------------------------------------------------
_issued_tokens: dict[str, float] = {}  # access_token → unix expiry


def _issue_token() -> tuple[str, int]:
    ttl = settings.oauth_token_ttl_seconds
    token = secrets.token_urlsafe(32)
    _issued_tokens[token] = time.time() + ttl
    return token, ttl


def _validate_issued_token(token: str) -> bool:
    expiry = _issued_tokens.get(token)
    if expiry is None:
        return False
    if time.time() >= expiry:
        # Expired — clean up
        _issued_tokens.pop(token, None)
        return False
    return True


# ---------------------------------------------------------------------------
# Request utilities
# ---------------------------------------------------------------------------
def _public_base_url(request: Request) -> str:
    """Reconstruct the scheme+host the client used to reach us.

    Behind Cloudflare Tunnel / Caddy / nginx the server sees an inbound
    http:// connection on loopback, not the public https:// URL. Prefer the
    forwarded headers so discovery URLs point at the same origin Claude
    hit (brain.gobag.dev), not at 127.0.0.1.
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto}://{host}"


# ---------------------------------------------------------------------------
# Path-rewrite middleware for MCP bare-path support
# ---------------------------------------------------------------------------
class McpPathNormalizeMiddleware:
    """Starlette's Mount('/mcp', ...) matches '/mcp/<x>' but returns 404 for
    exact '/mcp'. Claude iOS and other Streamable-HTTP clients POST to the
    bare '/mcp' URL, so we rewrite the scope path before routing."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            if scope.get("raw_path") == b"/mcp":
                scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
# Paths that bypass auth (discovery + health + token itself).
_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    "/token",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
})


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Protect /mcp (and any other non-public path) with Bearer auth.

    Accepts two token varieties:
      - a token issued by our own /token endpoint (the usual path for
        Claude iOS / web / Desktop via OAuth client_credentials)
      - optionally, a static dev-bypass token from settings.remote_bearer_token
        for curl testing
    """

    def __init__(self, app, static_token: str | None) -> None:
        super().__init__(app)
        self._static_token = static_token

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return self._challenge(request, error="invalid_request",
                                   description="missing bearer token")

        provided = header.split(" ", 1)[1].strip()

        # Dev-bypass static token (optional)
        if self._static_token and secrets.compare_digest(provided, self._static_token):
            return await call_next(request)

        # OAuth-issued token
        if _validate_issued_token(provided):
            return await call_next(request)

        return self._challenge(request, error="invalid_token",
                               description="token is not recognized or has expired")

    def _challenge(self, request: Request, *, error: str, description: str) -> Response:
        """Return a 401 with the WWW-Authenticate header MCP clients use to
        discover the OAuth protected-resource metadata (RFC 9728)."""
        base = _public_base_url(request)
        resource_metadata = f"{base}/.well-known/oauth-protected-resource"
        resp = JSONResponse(
            {"error": error, "error_description": description},
            status_code=401,
        )
        resp.headers["www-authenticate"] = (
            f'Bearer realm="goBrain", '
            f'error="{error}", '
            f'error_description="{description}", '
            f'resource_metadata="{resource_metadata}"'
        )
        return resp


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------
async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def _oauth_authorization_server(request: Request) -> JSONResponse:
    """RFC 8414 OAuth Authorization Server Metadata."""
    base = _public_base_url(request)
    return JSONResponse({
        "issuer": base,
        "token_endpoint": f"{base}/token",
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
        ],
        "grant_types_supported": ["client_credentials"],
        "response_types_supported": ["token"],
        "scopes_supported": ["mcp"],
    })


async def _oauth_protected_resource(request: Request) -> JSONResponse:
    """RFC 9728 Protected Resource Metadata — points MCP clients at our
    authorization server (which happens to be this same origin)."""
    base = _public_base_url(request)
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })


def _extract_client_credentials(
    form: Iterable, auth_header: str
) -> tuple[str | None, str | None]:
    """Read client_id + client_secret from either:
      - POST form body (client_secret_post), or
      - HTTP Basic auth header (client_secret_basic).
    """
    form = dict(form)
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")

    if auth_header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
            cid, _, csec = decoded.partition(":")
            client_id = client_id or cid
            client_secret = client_secret or csec
        except Exception:  # malformed header, ignore
            pass

    return client_id, client_secret


async def _token_endpoint(request: Request) -> JSONResponse:
    """OAuth 2.0 token endpoint — implements the client_credentials grant."""
    if not (settings.oauth_client_id and settings.oauth_client_secret):
        return JSONResponse(
            {"error": "server_error",
             "error_description": "OAuth credentials not configured on the server."},
            status_code=500,
        )

    try:
        form = await request.form()
    except Exception:
        form = {}

    grant_type = form.get("grant_type")
    if grant_type != "client_credentials":
        return JSONResponse(
            {"error": "unsupported_grant_type",
             "error_description": "only client_credentials is supported"},
            status_code=400,
        )

    auth_header = request.headers.get("authorization", "")
    client_id, client_secret = _extract_client_credentials(form, auth_header)

    if not client_id or not client_secret:
        return JSONResponse(
            {"error": "invalid_request",
             "error_description": "client_id and client_secret required"},
            status_code=400,
        )

    id_ok = secrets.compare_digest(client_id, settings.oauth_client_id)
    secret_ok = secrets.compare_digest(client_secret, settings.oauth_client_secret)
    if not (id_ok and secret_ok):
        log.warning("oauth_invalid_client", client_id_len=len(client_id))
        return JSONResponse(
            {"error": "invalid_client",
             "error_description": "unknown client or bad secret"},
            status_code=401,
        )

    token, ttl = _issue_token()
    log.info("oauth_token_issued", ttl_seconds=ttl, client_id=client_id)
    return JSONResponse({
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": ttl,
        "scope": "mcp",
    })


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------
def _build_app() -> Starlette:
    if not (settings.oauth_client_id and settings.oauth_client_secret):
        if not settings.remote_bearer_token:
            raise RuntimeError(
                "No auth configured. Set BRAIN_MCP_OAUTH_CLIENT_ID + "
                "BRAIN_MCP_OAUTH_CLIENT_SECRET in .env (for remote MCP clients), "
                "and/or BRAIN_MCP_REMOTE_BEARER_TOKEN (for curl testing). "
                "At least one must be set so the endpoint isn't open."
            )
        log.warning(
            "oauth_not_configured",
            note="running with only the static dev bearer token; Claude custom "
                 "connectors require OAuth",
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
                     port=settings.http_port,
                     oauth_configured=bool(settings.oauth_client_id))
            yield
            log.info("brain_mcp_http_stop")

    starlette_app = Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server",
                  _oauth_authorization_server, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource",
                  _oauth_protected_resource, methods=["GET"]),
            Route("/token", _token_endpoint, methods=["POST"]),
            Mount("/mcp", app=mcp_endpoint),
        ],
        middleware=[
            Middleware(BearerAuthMiddleware, static_token=settings.remote_bearer_token),
        ],
        lifespan=lifespan,
    )
    # Streamable HTTP clients POST to exactly /mcp. Default Starlette
    # routing 307-redirects that to /mcp/, which curl -X POST won't follow
    # and Claude iOS won't honor either. Turn it off on the router directly
    # (the Starlette constructor didn't accept this kwarg until ~0.38).
    starlette_app.router.redirect_slashes = False
    return starlette_app


def run() -> None:
    # Wrap the Starlette app in the path-normalizer so '/mcp' and '/mcp/'
    # both land on the Streamable HTTP session manager.
    asgi_app = McpPathNormalizeMiddleware(_build_app())
    uvicorn.run(
        asgi_app,
        host=settings.http_host,
        port=settings.http_port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
