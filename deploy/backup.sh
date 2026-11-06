#!/usr/bin/env bash
# pg_dump -Fc of the running db service into backups/. Keeps the newest $BACKUP_KEEP (default 14).
# From deploy.sh, cron or by hand. Without TAG it follows .last_good_tag (the images of the running stack).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -z "${TAG:-}" ] && [ -f .last_good_tag ]; then
    TAG=$(<.last_good_tag)
fi
# compose.prod.yaml only needs TAG to parse; the db service is the same for every tag
export TAG=${TAG:-none}
export COMPOSE_FILE=compose.yaml:deploy/compose.prod.yaml

umask 077  # dumps hold customer messages
mkdir -p backups
out="backups/momentum-$(date +%Y%m%d-%H%M%S).dump"

# -T: no tty (cron). The dump goes to a .part file, so a failed run never leaves a "good" name.
docker compose exec -T db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$out.part"
# a dump that pg_restore cannot list is useless: fail loudly now, not at restore time
docker compose exec -T db pg_restore -l < "$out.part" > /dev/null
mv "$out.part" "$out"

find backups -name 'momentum-*.dump' -printf '%T@ %p\n' | sort -rn | tail -n +$((${BACKUP_KEEP:-14} + 1)) \
    | cut -d' ' -f2- | xargs -r rm -f --
echo "backup ok: $out ($(du -h "$out" | cut -f1))"
