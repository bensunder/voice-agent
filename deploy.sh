#!/usr/bin/env bash
# Deploy / update the AI Sales Agent stack on this host.
#
#   ./deploy.sh              build + start (creates secrets and .env on first run)
#   ./deploy.sh --caddy      also install the Caddy site blocks (backup + validate + reload)
#   ./deploy.sh --status     show service health and integration status
#
# Safe to re-run: existing secrets and .env are never overwritten.
set -euo pipefail
cd "$(dirname "$0")"

APP_UID=10010   # matches the user in app/Dockerfile
PG_UID=70       # postgres user in postgres:16-alpine
CADDYFILE=/etc/caddy/Caddyfile

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null || die "missing required command: $1"; }
need docker; need openssl
docker compose version >/dev/null || die "docker compose plugin is required"

rand() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-"$1"; }

write_secret() {  # name value owner-uid   (never overwrites an existing secret)
  local f="secrets/$1"
  if [[ ! -e "$f" ]]; then
    printf '%s' "$2" > "$f"
    say "created secret $1"
  fi
  chown "$3:$3" "$f"
  chmod 600 "$f"
}

init_secrets() {
  mkdir -p secrets
  chmod 700 secrets
  if [[ ! -e secrets/db_password ]]; then
    local pw; pw="$(rand 40)"
    write_secret db_password "$pw" "$PG_UID"
    write_secret database_url "postgresql://salesagent:${pw}@db:5432/salesagent" "$APP_UID"
  fi
  [[ -s secrets/database_url ]] || die "secrets/database_url is missing; restore it or remove secrets/db_password to regenerate"
  write_secret tool_api_key "$(rand 48)" "$APP_UID"
  write_secret call_token_secret "$(rand 64)" "$APP_UID"
  write_secret cockpit_password "$(rand 20)" "$APP_UID"
  # Optional integrations: created empty; fill in when the tenant is ready.
  write_secret azure_client_secret "" "$APP_UID"
  write_secret power_automate_webhook_url "" "$APP_UID"
}

init_env() {
  if [[ ! -e .env ]]; then
    cp .env.example .env
    chmod 600 .env
    say "created .env from .env.example - edit it to add tenant settings"
  fi
}

install_caddy() {
  [[ -f "$CADDYFILE" ]] || die "$CADDYFILE not found"
  if grep -q '>>> ai-sales-agent >>>' "$CADDYFILE"; then
    say "Caddy blocks already present"
    return
  fi
  local bak; bak="$CADDYFILE.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$CADDYFILE" "$bak"
  printf '\n' >> "$CADDYFILE"
  cat deploy/Caddyfile.snippet >> "$CADDYFILE"
  if ! caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
    cp "$bak" "$CADDYFILE"
    die "Caddy validation failed; restored $bak. Run: caddy validate --config $CADDYFILE"
  fi
  systemctl reload caddy
  say "Caddy updated (backup: $bak)"
}

status() {
  docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
  printf '\nreadyz: '
  curl -fsS http://127.0.0.1:8310/readyz || echo "tool-api not ready"
  echo
}

wait_healthy() {
  say "waiting for services to become healthy"
  for _ in $(seq 1 60); do
    local unhealthy
    unhealthy="$(docker compose ps --format '{{.Service}} {{.Health}}' | awk '$2!="" && $2!="healthy"{print $1}')"
    [[ -z "$unhealthy" ]] && { say "all services healthy"; return 0; }
    sleep 2
  done
  docker compose ps
  docker compose logs --tail 50 tool-api cockpit worker
  die "services did not become healthy"
}

case "${1:-}" in
  --status) status; exit 0 ;;
esac

[[ $EUID -eq 0 ]] || die "run as root (needs to set secret file ownership)"
init_secrets
init_env
say "building and starting"
docker compose up -d --build --remove-orphans
wait_healthy
[[ "${1:-}" == "--caddy" ]] && install_caddy
status
echo
say "Cockpit:   https://cockpit.whyaidata.com   (user: $(grep -E '^COCKPIT_USERNAME=' .env | cut -d= -f2))"
say "Password:  cat $(pwd)/secrets/cockpit_password"
say "Tool API key for the Foundry connection:  cat $(pwd)/secrets/tool_api_key"
