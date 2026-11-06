#!/usr/bin/env bash
# Deploy on the server, from <deploy-dir> (after git pull):   deploy/deploy.sh
# preflight -> TAG=date-time-sha -> build -> backup -> migrate -> up --wait -> smoke -> .last_good_tag
# A failure after the stack was touched rolls back to the previous .last_good_tag.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export COMPOSE_FILE=compose.yaml:deploy/compose.prod.yaml
stage=preflight
previous=$(cat .last_good_tag 2>/dev/null || true)

env_value() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true; }

on_error() {
    echo "DEPLOY FAILED at stage: $stage" >&2
    case "$stage" in
        preflight | build | backup)
            echo "Nothing was changed on the running stack." >&2 ;;
        *)
            if [ -n "$previous" ]; then
                echo "Rolling back to $previous" >&2
                deploy/rollback.sh "$previous" >&2 || echo "ROLLBACK FAILED, fix by hand (docker compose logs)" >&2
                echo "If the migration had already changed the schema, restore the dump (deploy/README.md, section Rollback)." >&2
            else
                echo "First deploy: nothing to roll back to. See: docker compose logs" >&2
            fi ;;
    esac
    exit 1
}
trap on_error ERR

# ---- 1. preflight ---------------------------------------------------------------------------
[ -f .env ] || { echo ".env is missing (deploy/README.md, First deploy)" >&2; exit 1; }
[ "$(stat -c %a .env)" = 600 ] || { echo ".env must have mode 600 (chmod 600 .env)" >&2; exit 1; }
if grep -v '^[[:space:]]*#' .env | grep -q 'change-me'; then
    echo ".env still has change-me values:" >&2
    grep -v '^[[:space:]]*#' .env | grep 'change-me' | cut -d= -f1 >&2
    exit 1
fi
# the missing-variable messages come from the ${VAR:?} checks in compose.prod.yaml
TAG=preflight docker compose config --quiet
password=$(env_value POSTGRES_PASSWORD)
[[ "$password" =~ ^[0-9a-f]{32,}$ ]] \
    || { echo "POSTGRES_PASSWORD must be 32+ hex characters (openssl rand -hex 32): it goes into a URL" >&2; exit 1; }
operator=$(env_value OPERATOR_PASSWORD)
[ "${#operator}" -ge 16 ] || { echo "OPERATOR_PASSWORD is shorter than 16 characters" >&2; exit 1; }
case "$(env_value PUBLIC_BASE_URL)" in
    *localhost* | *127.0.0.1*) echo "PUBLIC_BASE_URL points at this host; drafts would link there" >&2; exit 1 ;;
    http://* | https://*) ;;
    *) echo "PUBLIC_BASE_URL must start with http:// or https://" >&2; exit 1 ;;
esac
[ -n "$(env_value ANTHROPIC_API_KEY)" ] || echo "note: ANTHROPIC_API_KEY is empty, the worker runs in no-key mode (static drafts)"
free_gb=$(df --output=avail -BG . | tail -n 1 | tr -dc '0-9')
[ "$free_gb" -ge 3 ] || { echo "Only ${free_gb} GB free, need 3+" >&2; exit 1; }

# ---- 2. tag ---------------------------------------------------------------------------------
TAG=$(date +%Y%m%d-%H%M%S)-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)
export TAG
echo "== deploying TAG=$TAG (previous: ${previous:-none})"

# ---- 3. build -------------------------------------------------------------------------------
stage=build
docker compose build

# ---- 4. backup before the schema changes (whenever the database already has tables) ----------
stage=backup
docker compose up -d --wait db
tables=$(docker compose exec -T db sh -c \
    'psql -qtAX -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from information_schema.tables where table_schema = '"'public'"'"')
if [ "${tables//[[:space:]]/}" != 0 ]; then
    deploy/backup.sh
else
    echo "empty database: backup skipped"
fi

# ---- 5. migrate (schema + KB seed, both idempotent) ---------------------------------------------
stage=migrate
docker compose run --rm --no-deps migrate

# ---- 6. start: waits for the healthchecks, at most 120 s ----------------------------------------
stage=up
docker compose up -d --wait --wait-timeout 120 --no-deps --remove-orphans db api worker mock-helpdesk

# ---- 7. smoke through the published ports -------------------------------------------------------
stage=smoke
deploy/smoke.sh

# ---- 8. done ----------------------------------------------------------------------------------
echo "$TAG" > .last_good_tag
echo "== deploy ok: $TAG"
docker compose ps
