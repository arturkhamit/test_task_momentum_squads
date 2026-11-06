#!/usr/bin/env bash
# Back to a built tag: deploy/rollback.sh [TAG]   (default: the one in .last_good_tag)
# Only the containers go back. Migrations are not undone: if the new version changed the schema
# in a way the old code cannot read, restore the dump taken before the deploy (deploy/README.md).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

TAG=${1:-$(cat .last_good_tag 2>/dev/null || true)}
[ -n "$TAG" ] || { echo "no TAG given and no .last_good_tag" >&2; exit 1; }
export TAG
export COMPOSE_FILE=compose.yaml:deploy/compose.prod.yaml

docker image inspect "momentum-squads:$TAG" > /dev/null \
    || { echo "image momentum-squads:$TAG does not exist (docker image ls momentum-squads)" >&2; exit 1; }

echo "== rolling back to $TAG"
# --no-deps and no `migrate`: the old image's alembic would fail on a schema it does not know
docker compose up -d --wait --wait-timeout 120 --no-build --no-deps db api worker mock-helpdesk
deploy/smoke.sh
echo "$TAG" > .last_good_tag
echo "== rollback ok: $TAG"
