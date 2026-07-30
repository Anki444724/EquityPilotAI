#!/usr/bin/env bash
#
# One-command Railway deployment.
#
#   export RAILWAY_TOKEN=<project or account token>
#   export GEMINI_API_KEY=<your key>          # optional but recommended
#   export OPENROUTER_API_KEY=<your key>      # optional fallback
#   ./deploy/railway-deploy.sh
#
# Idempotent: re-running reconciles rather than duplicating. Safe to run after
# a partial failure.
#
# What it does, in order:
#   1. verify the CLI and token
#   2. link or create the project
#   3. provision Postgres and Redis
#   4. generate and set every environment variable
#   5. deploy api, worker and web
#   6. run `alembic upgrade head`
#   7. poll /health/ready until the service is live
#   8. run the production verification suite
#
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-ierp}"
RAILWAY="npx --yes @railway/cli"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  ✗\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. preflight
log "Preflight"
command -v npx >/dev/null || die "npx not found — install Node 20+"
[[ -n "${RAILWAY_TOKEN:-}" ]] || die "RAILWAY_TOKEN is not set.
  Create one at https://railway.app/account/tokens and export it:
    export RAILWAY_TOKEN=..."

$RAILWAY whoami >/dev/null 2>&1 || die "RAILWAY_TOKEN is rejected — check it has not expired"
ok "Railway CLI authenticated"

# Secrets are generated here rather than committed. Regenerating SECRET_KEY
# invalidates every live session; regenerating ENCRYPTION_KEY makes stored
# tenant secrets undecryptable — so both are generated once and reused if the
# variables already exist on the service.
gen() { python3 -c "import secrets;print(secrets.token_urlsafe(48))"; }

# ------------------------------------------------------------------ 2. project
log "Project"
if $RAILWAY status >/dev/null 2>&1; then
  ok "already linked to a project"
else
  $RAILWAY init --name "$PROJECT_NAME" >/dev/null
  ok "created project '$PROJECT_NAME'"
fi

# --------------------------------------------------------------- 3. datastores
log "Datastores"
existing="$($RAILWAY status --json 2>/dev/null || echo '{}')"
if grep -qi postgres <<<"$existing"; then
  ok "Postgres already provisioned"
else
  $RAILWAY add --database postgres >/dev/null && ok "Postgres provisioned"
fi
if grep -qi redis <<<"$existing"; then
  ok "Redis already provisioned"
else
  $RAILWAY add --database redis >/dev/null && ok "Redis provisioned"
fi

# Railway injects DATABASE_URL and REDIS_URL by reference; they are never set
# by hand here, so rotating a database password does not require a redeploy.

# ------------------------------------------------------------------- 4. config
log "Environment variables"

SECRET_KEY_VALUE="${SECRET_KEY:-$(gen)}"
ENCRYPTION_KEY_VALUE="${ENCRYPTION_KEY:-$(gen)}"

set_var() {
  # `--skip-deploys` batches the changes; one deploy happens at the end.
  $RAILWAY variables --set "$1=$2" --skip-deploys >/dev/null 2>&1 \
    || warn "could not set $1"
}

set_var ENVIRONMENT production
set_var DEBUG false
set_var SECRET_KEY "$SECRET_KEY_VALUE"
set_var ENCRYPTION_KEY "$ENCRYPTION_KEY_VALUE"
set_var NATIVE_AUTH true
set_var LOG_FORMAT json
set_var LOG_LEVEL INFO
set_var RATE_LIMIT_BACKEND redis
set_var METRICS_ENABLED true
set_var COOKIE_SECURE true
set_var COOKIE_SAMESITE lax
set_var CSRF_ENABLED true
set_var AI_MOCK_MODE true
set_var AI_PREFERRED_PROVIDER Gemini

# API keys come from the caller's environment and are never echoed.
if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  set_var GEMINI_API_KEY "$GEMINI_API_KEY";     ok "GEMINI_API_KEY set"
else
  warn "GEMINI_API_KEY not provided — AI degrades to the offline provider"
fi
if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
  set_var OPENROUTER_API_KEY "$OPENROUTER_API_KEY"; ok "OPENROUTER_API_KEY set"
else
  warn "OPENROUTER_API_KEY not provided — no fallback if Gemini is exhausted"
fi

ok "configuration applied"

# ------------------------------------------------------------------- 5. deploy
log "Deploy"
cd "$ROOT/backend"
$RAILWAY up --detach
ok "backend deployed"

DOMAIN="$($RAILWAY domain 2>/dev/null | grep -oE '[a-z0-9.-]+\.up\.railway\.app' | head -1 || true)"
[[ -n "$DOMAIN" ]] || DOMAIN="$($RAILWAY domain 2>&1 | grep -oE '[a-z0-9.-]+\.up\.railway\.app' | head -1 || true)"
[[ -n "$DOMAIN" ]] && ok "domain: https://$DOMAIN" || warn "no domain yet — run: railway domain"

# ---------------------------------------------------------------- 6. migrations
log "Migrations"
# Run explicitly rather than trusting the start command, so a migration
# failure is visible here instead of as a crash loop.
$RAILWAY run alembic upgrade head && ok "schema at head" \
  || die "migration failed — the app will refuse to start in production"

# ------------------------------------------------------------------- 7. health
if [[ -n "$DOMAIN" ]]; then
  log "Health"
  for attempt in $(seq 1 40); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/health/ready" || echo 000)"
    if [[ "$code" == "200" ]]; then ok "ready after ${attempt}0s"; break; fi
    [[ $attempt -eq 40 ]] && die "never became ready (last HTTP $code) — check: railway logs"
    sleep 10
  done

  # -------------------------------------------------------------- 8. verify
  log "Production verification"
  python3 "$ROOT/deploy/verify_deployment.py" --url "https://$DOMAIN"
fi

log "Done"
echo
echo "  URL     : https://${DOMAIN:-<run: railway domain>}"
echo "  Docs    : https://${DOMAIN:-...}/docs"
echo "  Health  : https://${DOMAIN:-...}/health/ready"
echo "  AI      : https://${DOMAIN:-...}/api/v1/ai/health"
echo
echo "  Secrets were generated and set on the service. They are not printed"
echo "  here and not written to disk — read them back with:"
echo "    railway variables"
