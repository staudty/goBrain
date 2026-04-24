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

`brain-mcp-http` speaks MCP's OAuth 2.0 auth flow out of the box — the
same one Anthropic's Custom Connector UI expects. Claude presents a
pre-shared `client_id` + `client_secret`, this server's `/token`
endpoint validates the pair and issues a short-lived Bearer access
token, Claude uses that token on `/mcp` calls.

On the always-on host (the one that runs the ingester), in
`mcp-server/.env`:

```bash
# Existing settings stay as-is (Postgres DSN, Ollama URL, etc.)

BRAIN_MCP_HTTP_HOST=127.0.0.1
BRAIN_MCP_HTTP_PORT=8766

# OAuth 2.0 client_credentials — paste these into Claude's "OAuth
# Client ID" and "OAuth Client Secret" fields when adding the connector.
# Generate each with: openssl rand -hex 32
BRAIN_MCP_OAUTH_CLIENT_ID=<generate>
BRAIN_MCP_OAUTH_CLIENT_SECRET=<generate>

# Optional: a static bypass token just for curl testing.
BRAIN_MCP_REMOTE_BEARER_TOKEN=<optional>
```

`http_server.py` refuses to start if neither OAuth credentials nor a
static bearer token is set, so the endpoint is never accidentally open.

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

Install as a service — **but note the macOS gotcha**:
```bash
sudo cloudflared service install
```

On macOS, `service install` creates a LaunchDaemon stub with **no tunnel
arguments** and points at `/etc/cloudflared/config.yml` (a root-owned
path that doesn't exist yet). Two fixes are needed before the daemon
can actually run your tunnel:

```bash
# 1. Copy config + credentials from your user home into the system path
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/config.yml /etc/cloudflared/
sudo cp ~/.cloudflared/<tunnel-uuid>.json /etc/cloudflared/

# 2. Update the daemon plist's ProgramArguments so it actually runs the tunnel.
#    Edit /Library/LaunchDaemons/com.cloudflare.cloudflared.plist and make
#    ProgramArguments look like:
#      /opt/homebrew/bin/cloudflared
#      --config
#      /etc/cloudflared/config.yml
#      tunnel
#      run

# 3. Reload
sudo launchctl unload /Library/LaunchDaemons/com.cloudflare.cloudflared.plist
sudo launchctl load /Library/LaunchDaemons/com.cloudflare.cloudflared.plist
```

Verify with `curl https://brain.your-domain.tld/health` — should
return `{"ok":true}` once the tunnel registers with Cloudflare (5-10s).

> On Linux with systemd, `cloudflared service install` Just Works and
> reads `/etc/cloudflared/config.yml` directly. The macOS stub plist
> quirk is specific to the Homebrew / LaunchDaemon path.

For extra security, optionally turn on **Cloudflare Access** on that
hostname with a Google/GitHub/email-OTP policy — then bearer token and
Access both gate the endpoint.

## 4. Smoke-test publicly

From anywhere (your phone on cellular is the real test):

```bash
# health
curl https://brain.your-domain.tld/health
# {"ok":true}

# unauthenticated /mcp returns a challenge pointing at OAuth metadata
curl -i -X POST https://brain.your-domain.tld/mcp
# HTTP/1.1 401 Unauthorized
# www-authenticate: Bearer realm="goBrain", error="invalid_request", ... resource_metadata="https://brain.your-domain.tld/.well-known/oauth-protected-resource"

# OAuth discovery
curl https://brain.your-domain.tld/.well-known/oauth-protected-resource
curl https://brain.your-domain.tld/.well-known/oauth-authorization-server

# client_credentials grant
curl -X POST https://brain.your-domain.tld/token \
     -d "grant_type=client_credentials&client_id=$BRAIN_MCP_OAUTH_CLIENT_ID&client_secret=$BRAIN_MCP_OAUTH_CLIENT_SECRET"
# {"access_token":"…","token_type":"Bearer","expires_in":3600,"scope":"mcp"}

# tools/list with the issued token
TOKEN=<copy access_token from above>
curl -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -X POST https://brain.your-domain.tld/mcp \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# Streamable HTTP initialization + list of tools
```

If all of those succeed, the server is publicly reachable and OAuth is working.

## 5. Register as a connector in Claude

The one-time setup is on **claude.ai (web)** because the iOS app doesn't
expose the custom-connector add flow in most versions. Once registered
against your Claude account, the connector shows up automatically on
iOS, Desktop, and the web UI.

1. Open claude.ai → profile menu → **Settings → Connectors**.
2. Click **Add custom connector**.
3. Name: `goBrain` (or anything).
4. Remote MCP server URL: `https://brain.your-domain.tld/mcp`
5. Expand **Advanced settings** and paste:
   - **OAuth Client ID:** `$BRAIN_MCP_OAUTH_CLIENT_ID`
   - **OAuth Client Secret:** `$BRAIN_MCP_OAUTH_CLIENT_SECRET`
6. Click **Add**.

Claude probes the OAuth discovery URL, exchanges the credentials at
`/token`, fetches `tools/list`, and presents the three tools for you
to enable.

## 6. Using it from the iOS app

Once connected, ask Claude things like:

> Use search_brain to pull up what we decided about the auth migration last week.

> What's in my brain about the Postgres upgrade plan?

Claude will call `search_brain` with your query, get back the curated
top-N chunks from your vault, and reason over those. The vault itself
never leaves your LAN — only the curated chunks do.

## Security posture

- **Authentication.** Pre-shared `client_id` + `client_secret` exchanged
  for short-lived Bearer access tokens via OAuth 2.0 client_credentials.
  256-bit random values (`openssl rand -hex 32`) are effectively
  unbrute-forceable. Tokens live in memory and expire after
  `BRAIN_MCP_OAUTH_TOKEN_TTL_SECONDS` (default 3600). Restart invalidates
  all outstanding tokens; clients silently re-issue.
- **Transport.** TLS terminates at the reverse proxy; the server itself
  only accepts loopback traffic.
- **Scope.** The server exposes the same tools as stdio — read-only
  retrieval against your vault. No write endpoints.
- **Rotation.** To rotate, generate new `BRAIN_MCP_OAUTH_CLIENT_ID` and
  `BRAIN_MCP_OAUTH_CLIENT_SECRET` values in `.env`, restart with
  `launchctl kickstart -k gui/$(id -u)/com.gobag.mcp-remote`, then update
  the credentials in claude.ai's connector settings. All issued access
  tokens are invalidated on restart.
