# Security

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the repository owner. Do not open a public
issue. Include the affected component, steps to reproduce and the potential impact.

## Secrets

- Secrets are never committed. They live as files in `secrets/` on the host (mode 600, owned by
  the service user) and are mounted read-only at `/run/secrets` in the containers.
- `.env` holds non-secret configuration only and is excluded from version control.
- `deploy.sh` never overwrites an existing secret.

## Rotation

| Secret | How to rotate | Also update |
|---|---|---|
| `tool_api_key` | Replace the file, `docker compose up -d` | The Foundry MCP tool connection (and any OpenAPI tool) |
| `call_token_secret` | Replace the file, restart | Nothing; in-flight call tokens become invalid |
| `cockpit_password` | Replace the file, restart | Operators |
| `azure_client_secret` | New secret in the Entra app registration, replace the file, restart | Remove the old secret in Entra |
| `power_automate_webhook_url` | Regenerate the flow trigger URL, replace the file, restart | - |
| `db_password` | Change the Postgres role password and update `database_url` together | - |

## Hardening summary

Loopback-only service ports behind a TLS proxy that exposes only the tool surface; Postgres on an
internal network; read-only, non-root containers with all capabilities dropped; HMAC per-call
tokens so the voice agent can only act on the call it is on; Pydantic validation on every tool
argument; append-only audit log.
