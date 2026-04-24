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
  - Implements both OAuth 2.0 grant types MCP clients use in the wild:
        * authorization_code + PKCE (what Claude iOS / web / Desktop use)
        * client_credentials        (handy for automation / curl tests)

Anthropic's Custom Connector UI asks for an OAuth Client ID + Client Secret.
Paste the values from .env into those fields on claude.ai; Claude handles
the rest of the OAuth dance on every invocation.

Single-user server: /authorize does not render a consent screen — it
immediately issues a code and redirects back. The redirect_uri allowlist
in settings.oauth_allowed_redirect_uris prevents someone tricking your
browser into leaking a code to a third-party target; PKCE prevents code
reuse if one ever does leak.

For local curl testing there is an optional BRAIN_MCP_REMOTE_BEARER_TOKEN
static token that bypasses OAuth entirely. Treat it as an admin credential.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import secrets
import time
from typing import Iterable
from urllib.parse import urlencode

import structlog
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from .config import settings
from .server import app as mcp_app

logging.basicConfig(level=logging.INFO)
log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# In-memory access-token + authorization-code stores
#
# A restart invalidates both; MCP clients re-run the grant on the next call
# and carry on. No persistent user session to lose.
# ---------------------------------------------------------------------------
_issued_tokens: dict[str, float] = {}  # access_token → unix expiry
_issued_codes: dict[str, dict] = {}    # authorization_code → {..., "expires_at": float}


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
        _issued_tokens.pop(token, None)
        return False
    return True


def _issue_code(*, client_id: str, redirect_uri: str,
                code_challenge: str, code_challenge_method: str,
                scope: str) -> str:
    code = secrets.token_urlsafe(32)
    _issued_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scope": scope,
        "expires_at": time.time() + settings.oauth_code_ttl_seconds,
    }
    return code


def _consume_code(code: str) -> dict | None:
    """Pop-and-validate an authorization_code. One-time use by design."""
    entry = _issued_codes.pop(code, None)
    if entry is None:
        return None
    if time.time() >= entry["expires_at"]:
        return None
    return entry


def _verify_pkce(code_verifier: str, expected_challenge: str, method: str) -> bool:
    if method != "S256":
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, expected_challenge)


# ---------------------------------------------------------------------------
# Request utilities
# ---------------------------------------------------------------------------
def _public_base_url(request: Request) -> str:
    """Reconstruct the scheme+host the client used to reach us.

    Behind Cloudflare Tunnel / Caddy / nginx the server sees an inbound
    http:// connection on loopback, not the public https:// URL. Prefer the
    forwarded headers so discovery URLs point at the same origin the client
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
_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    "/token",
    "/authorize",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",  # Claude probes this variant too
})


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Protect /mcp (and any non-public path) with Bearer auth.

    Accepts either a token issued by our own /token endpoint (the normal
    path for Claude iOS / web / Desktop via OAuth) or, optionally, a static
    dev-bypass token from settings.remote_bearer_token for curl testing.
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

        if self._static_token and secrets.compare_digest(provided, self._static_token):
            return await call_next(request)

        if _validate_issued_token(provided):
            return await call_next(request)

        return self._challenge(request, error="invalid_token",
                               description="token is not recognized or has expired")

    def _challenge(self, request: Request, *, error: str, description: str) -> Response:
        """Return 401 with the WWW-Authenticate header MCP clients follow
        to discover the OAuth protected-resource metadata (RFC 9728)."""
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
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
            "none",  # public clients using PKCE
        ],
        "grant_types_supported": [
            "authorization_code",
            "client_credentials",
        ],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["mcp"],
    })


async def _oauth_protected_resource(request: Request) -> JSONResponse:
    """RFC 9728 Protected Resource Metadata."""
    base = _public_base_url(request)
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })


def _error_redirect(redirect_uri: str, error: str, state: str | None,
                    description: str | None = None) -> RedirectResponse:
    """Build a spec-compliant error redirect back to the client."""
    params = {"error": error}
    if description:
        params["error_description"] = description
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


async def _authorize_endpoint(request: Request) -> Response:
    """OAuth 2.0 authorization endpoint — authorization_code grant + PKCE.

    Single-user server: no consent screen. We validate client_id,
    redirect_uri, and PKCE parameters; on success we issue an auth code
    and redirect straight back to the client's callback.
    """
    params = request.query_params
    response_type = params.get("response_type")
    client_id = params.get("client_id") or ""
    redirect_uri = params.get("redirect_uri") or ""
    code_challenge = params.get("code_challenge") or ""
    code_challenge_method = params.get("code_challenge_method") or "plain"
    state = params.get("state")
    scope = params.get("scope", "mcp")

    # Validate redirect_uri FIRST — don't redirect errors to an unvalidated URI.
    if redirect_uri not in settings.oauth_allowed_redirect_uris:
        log.warning("authorize_redirect_uri_rejected", got=redirect_uri)
        return JSONResponse(
            {"error": "invalid_request",
             "error_description": f"redirect_uri not in server allowlist: {redirect_uri}"},
            status_code=400,
        )

    # Validate client_id similarly — direct 400, don't leak to redirect target.
    expected_id = settings.oauth_client_id or ""
    if not expected_id or not secrets.compare_digest(client_id, expected_id):
        log.warning("authorize_invalid_client_id", got_len=len(client_id))
        return JSONResponse(
            {"error": "invalid_client",
             "error_description": "unknown client_id"},
            status_code=400,
        )

    # Beyond here, errors are safe to redirect back (client_id + redirect_uri are OK).
    if response_type != "code":
        return _error_redirect(redirect_uri, "unsupported_response_type", state)

    if not code_challenge or code_challenge_method != "S256":
        return _error_redirect(
            redirect_uri, "invalid_request", state,
            "PKCE with code_challenge_method=S256 is required",
        )

    code = _issue_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        scope=scope,
    )
    log.info("oauth_code_issued", scope=scope, redirect_uri=redirect_uri)

    callback_params = {"code": code}
    if state:
        callback_params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}{urlencode(callback_params)}",
        status_code=302,
    )


def _extract_client_credentials(
    form: Iterable, auth_header: str
) -> tuple[str | None, str | None]:
    form = dict(form)
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")

    if auth_header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
            cid, _, csec = decoded.partition(":")
            client_id = client_id or cid
            client_secret = client_secret or csec
        except Exception:
            pass

    return client_id, client_secret


async def _token_endpoint(request: Request) -> JSONResponse:
    """OAuth 2.0 token endpoint. Supports:
      - grant_type=authorization_code + code + redirect_uri + code_verifier
      - grant_type=client_credentials + client_id + client_secret
    """
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
    form_dict = dict(form)
    auth_header = request.headers.get("authorization", "")
    grant_type = form_dict.get("grant_type")

    # ---- authorization_code grant -----------------------------------------
    if grant_type == "authorization_code":
        code = form_dict.get("code")
        redirect_uri = form_dict.get("redirect_uri")
        code_verifier = form_dict.get("code_verifier")
        client_id, client_secret = _extract_client_credentials(form_dict, auth_header)

        if not (code and redirect_uri and code_verifier and client_id):
            return JSONResponse(
                {"error": "invalid_request",
                 "error_description": "code, redirect_uri, code_verifier, client_id required"},
                status_code=400,
            )

        entry = _consume_code(code)
        if entry is None:
            return JSONResponse(
                {"error": "invalid_grant",
                 "error_description": "code is unknown, expired, or already used"},
                status_code=400,
            )
        if entry["client_id"] != client_id:
            return JSONResponse(
                {"error": "invalid_grant",
                 "error_description": "code was issued to a different client"},
                status_code=400,
            )
        if entry["redirect_uri"] != redirect_uri:
            return JSONResponse(
                {"error": "invalid_grant",
                 "error_description": "redirect_uri does not match the /authorize request"},
                status_code=400,
            )
        if not _verify_pkce(code_verifier, entry["code_challenge"],
                            entry["code_challenge_method"]):
            return JSONResponse(
                {"error": "invalid_grant",
                 "error_description": "PKCE verification failed"},
                status_code=400,
            )
        # Confidential client: if a secret is supplied, it must match.
        if client_secret and not secrets.compare_digest(
            client_secret, settings.oauth_client_secret or ""
        ):
            return JSONResponse(
                {"error": "invalid_client",
                 "error_description": "bad client_secret"},
                status_code=401,
            )

        token, ttl = _issue_token()
        log.info("oauth_token_issued", grant="authorization_code",
                 ttl_seconds=ttl, scope=entry.get("scope"))
        return JSONResponse({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": ttl,
            "scope": entry.get("scope", "mcp"),
        })

    # ---- client_credentials grant -----------------------------------------
    if grant_type == "client_credentials":
        client_id, client_secret = _extract_client_credentials(form_dict, auth_header)
        if not client_id or not client_secret:
            return JSONResponse(
                {"error": "invalid_request",
                 "error_description": "client_id and client_secret required"},
                status_code=400,
            )
        id_ok = secrets.compare_digest(client_id, settings.oauth_client_id or "")
        secret_ok = secrets.compare_digest(client_secret, settings.oauth_client_secret or "")
        if not (id_ok and secret_ok):
            log.warning("oauth_invalid_client", client_id_len=len(client_id))
            return JSONResponse(
                {"error": "invalid_client",
                 "error_description": "unknown client or bad secret"},
                status_code=401,
            )
        token, ttl = _issue_token()
        log.info("oauth_token_issued", grant="client_credentials", ttl_seconds=ttl)
        return JSONResponse({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": ttl,
            "scope": "mcp",
        })

    return JSONResponse(
        {"error": "unsupported_grant_type",
         "error_description": "only authorization_code and client_credentials are supported"},
        status_code=400,
    )


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
            # Some clients probe an /mcp-scoped variant of the resource metadata.
            Route("/.well-known/oauth-protected-resource/mcp",
                  _oauth_protected_resource, methods=["GET"]),
            Route("/authorize", _authorize_endpoint, methods=["GET"]),
            Route("/token", _token_endpoint, methods=["POST"]),
            Mount("/mcp", app=mcp_endpoint),
        ],
        middleware=[
            Middleware(BearerAuthMiddleware, static_token=settings.remote_bearer_token),
        ],
        lifespan=lifespan,
    )
    # Streamable HTTP clients POST to exactly /mcp. Default Starlette routing
    # 307-redirects that to /mcp/, which curl -X POST won't follow and Claude
    # iOS won't honor either.
    starlette_app.router.redirect_slashes = False
    return starlette_app


def run() -> None:
    asgi_app = McpPathNormalizeMiddleware(_build_app())
    uvicorn.run(
        asgi_app,
        host=settings.http_host,
        port=settings.http_port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
