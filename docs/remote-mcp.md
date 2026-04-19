# Remote MCP — exposing goBrain to Claude iOS / cloud clients

The stdio MCP server (`brain-mcp`) runs as a child process of each local
client. Mobile apps and the Claude web app can't spawn local processes —
they talk to MCP servers over HTTPS. `brain-mcp-http` is the HTTP-transport
variant that lets you wire up `search_brain` as a **Custom Connector** in
Claude iOS, Claude.ai web, or any other remote-MCP-capable client.

Architecture:

```
iPhone / Claude.ai
        │
        ▼  https://brain.<your-domain>/mcp  (Bearer <token>)
   TLS reverse proxy   (Caddy, Cloudflare Tunnel, nginx, …)
        │
        ▼  http://127.0.0.1:8766/mcp
   brain-mcp-http      (Streamable HTTP + bearer auth)
        │
        ├──► Postgres (pgvector) — same DB as stdio server
        └──► Ollama — embed + rerank
```

The HTTP server reuses the exact same tool implementations as the stdio
server (`server.py`). Both transports share one source of truth.

## 1. Configure

On the always-on host (the one that runs the ingester), in
`mcp-server/.env`:

```bash
# Existing settings stay as-is (Postgres DSN, Ollama URL, etc.)

# New ones:
BRAIN_MCP_HTTP_HOST=127.0.0.1
BRAIN_MCP_HTTP_PORT=8766
BRAIN_MCP_REMOTE_BEARER_TOKEN=<generate with: openssl rand -hex 32>
```

The token is what Claude iOS will present as `Authorization: Bearer <token>`.
Keep it out of anywhere public. `http_server.py` refuses to start if this
value is empty — no accidental unauthenticated endpoints.

## 2. Install as a LaunchAgent (macOS)

```bash
cd mac-mini
./setup-mcp-remote.sh
```

The script `uv sync`s the mcp-server package, renders
`launchd/com.gobag.mcp-remote.plist` with your `$HOME` + repo path,
loads it with `launchctl`, and waits for `/health` on
`127.0.0.1:${BRAIN_MCP_HTTP_PORT}`.

Logs:
```
tail -f ~/Library/Logs/gobag-mcp-remote.out.log
tail -f ~/Library/Logs/gobag-mcp-remote.err.log
```

## 3. Put a TLS reverse proxy in front

The server binds to loopback only — you must front it with something
that terminates TLS and is reachable from the internet. Two common
options:

### Option A — Caddy (if you already run one for your domain)

Add to your existing `Caddyfile`:

```caddyfile
brain.your-domain.tld {
    reverse_proxy 127.0.0.1:8766 {
        # If Caddy runs on a different host than the MCP server, swap the
        # target for that host's IP (and make the MCP server bind
        # 0.0.0.0 instead of 127.0.0.1 — OR put them on a Tailscale mesh).
    }
}
```

Caddy handles Let's Encrypt automatically. No other config needed.

### Option B — Cloudflare Tunnel

If your domain's on Cloudflare, this avoids opening any port in your
router:

```bash
brew install cloudflared
cloudflared tunnel login                       # browser-based login
cloudflared tunnel create goBrain
cloudflared tunnel route dns goBrain brain.your-domain.tld
```

Then `~/.cloudflared/config.yml`:
```yaml
tunnel: goBrain
credentials-file: /Users/<you>/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: brain.your-domain.tld
    service: http://127.0.0.1:8766
  - service: http_status:404
```

Install as a service:
```bash
sudo cloudflared service install
```

For extra security, optionally turn on **Cloudflare Access** on that
hostname with a Google/GitHub/email-OTP policy — then bearer token and
Access both gate the endpoint.

## 4. Smoke-test publicly

From anywhere (your phone on cellular is the real test):

```bash
curl https://brain.your-domain.tld/health
# {"ok": true}

curl -H "Authorization: Bearer WRONG" https://brain.your-domain.tld/mcp
# {"error":"invalid token"}

curl -H "Authorization: Bearer $BRAIN_MCP_REMOTE_BEARER_TOKEN" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -X POST https://brain.your-domain.tld/mcp \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# Streamable HTTP initialization + list of tools
```

If all three succeed, the server is publicly reachable and auth is working.

## 5. Register as a connector in Claude iOS

1. Open the Claude iOS app.
2. Settings → Connectors → **Add custom connector**.
3. URL: `https://brain.your-domain.tld/mcp`
4. Name: anything you like (e.g. "My second brain").
5. Auth: choose **Bearer token** / **API key** and paste the token value.
6. Save — it'll probe the endpoint, list the tools, and ask which to enable.

Same flow works on Claude.ai web (Settings → Integrations → Custom
integrations) and Claude Desktop's connectors UI.

## 6. Using it from the iOS app

Once connected, ask Claude things like:

> Use search_brain to pull up what we decided about the auth migration last week.

> What's in my brain about the Postgres upgrade plan?

The iOS app will call `search_brain` with your query, get back the
curated top-N chunks from your vault, and reason over those. The vault
itself never leaves your LAN — only the curated chunks do.

## Security posture

- **Authentication.** Bearer token (256-bit random via `openssl rand -hex 32`
  recommended). If you run Cloudflare Access on top, the endpoint also
  requires a successful OAuth login — that's belt-and-suspenders.
- **Transport.** TLS terminates at the reverse proxy; the server itself
  only accepts loopback traffic.
- **Scope.** The server exposes the same tools as stdio — read-only
  retrieval against your vault. No write endpoints.
- **Rotation.** To rotate, generate a new `BRAIN_MCP_REMOTE_BEARER_TOKEN`,
  restart with `launchctl kickstart -k gui/$(id -u)/com.gobag.mcp-remote`,
  then update the token in the Claude iOS connector settings.
