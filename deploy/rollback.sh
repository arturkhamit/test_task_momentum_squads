#!/usr/bin/env bash
# Back to a built tag: deploy/rollback.sh [TAG]   (default: .previous_tag, the release before the current one)
# Afterwards .last_good_tag and .previous_tag are swapped, so running it again returns to where you were.
# Only the containers go back. Migrations are not undone: if the new version changed the schema
# in a way the old code cannot read, restore the dump taken before the deploy (deploy/README.md).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

current=$(cat .last_good_tag 2>/dev/null || true)
TAG=${1:-$(cat .previous_tag 2>/dev/null || true)}
[ -n "$TAG" ] || { echo "no TAG given and no .previous_tag: there is no earlier release to go back to" >&2; exit 1; }
export TAG
export COMPOSE_FILE=compose.yaml:deploy/compose.prod.yaml

docker image inspect "momentum-squads:$TAG" > /dev/null \
    || { echo "image momentum-squads:$TAG does not exist (docker image ls momentum-squads)" >&2; exit 1; }

echo "== rolling back to $TAG (current: ${current:-none})"
# --no-deps and no `migrate`: the old image's alembic would fail on a schema it does not know
docker compose up -d --wait --wait-timeout 120 --no-build --no-deps db api worker mock-helpdesk
deploy/smoke.sh
# deploy.sh's own rollback passes the current .last_good_tag: nothing to swap then
if [ "$TAG" != "$current" ]; then
    [ -z "$current" ] || echo "$current" > .previous_tag
    echo "$TAG" > .last_good_tag
fi
echo "== rollback ok: $TAG (previous release: $(cat .previous_tag 2>/dev/null || echo none))"
