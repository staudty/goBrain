# Security

## Reporting a vulnerability

If you've found a security issue, **don't open a public issue.** Email the
maintainer (contact in the repo's About section) with a description and
reproduction steps. We'll acknowledge within a week.

## Threat model (in scope)

goBrain is designed to be deployed on a trusted private network (home LAN
or a VPN-connected mesh like Tailscale). Within that scope, we care about:

- **Credential hygiene** — no secrets committed to the repo. `.env` files are
  gitignored; only `.env.example` with placeholders is committed.
- **Local privilege** — nothing in the project needs root on the host
  beyond the Postgres container's own operation.
- **Data confidentiality at rest** — the vault + Postgres DB should be
  backed up encrypted if they leave the local network (e.g., to S3/B2).
  See `docs/runbook.md` for backup guidance.

## Out of scope

- **Internet-exposed deployments.** The ingester's HTTP API and Ollama are
  unauthenticated by design — they assume a trusted network. If you need to
  expose them over the public internet, put them behind a reverse proxy
  with auth (Caddy + OAuth, Cloudflare Tunnel with Access, etc.) and
  file any resulting concerns as separate issues.
- **Multi-tenant usage.** goBrain is single-user software today.

## Known operational risks

- The Ollama server binds to `0.0.0.0:11434` by default so cross-host MCP
  servers can reach it. On untrusted networks, bind to `127.0.0.1` or the
  specific LAN interface, or put it behind an auth proxy.
- Postgres is exposed on port 5433 (non-standard) without pgHBA auth
  restrictions by default. Change the password (`POSTGRES_PASSWORD` in
  your `.env`) and consider `pg_hba.conf` rules if you're on a shared LAN.
- SQLite buffer file (`~/.goBrain/buffer.sqlite`) contains unsummarized
  conversation content until Postgres drains it. Treat that directory with
  the same sensitivity as the vault.
