# Deploy to the server

Placeholders: `<ssh-user>@<server-ip>` is the server, `<deploy-dir>` the checkout on it (e.g. `~/apps/momentum_squads`).
The commands take them from shell variables, so set these once per terminal:

```
export DEPLOY_HOST=<ssh-user>@<server-ip>      # on the laptop
export SERVER_IP=<server-ip> DEPLOY_DIR=<deploy-dir>   # on the server, e.g. DEPLOY_DIR=~/apps/momentum_squads
```

A rehearsal on the laptop cannot use `SERVER_IP=127.0.0.1`: preflight refuses a localhost `PUBLIC_BASE_URL` by
design. Use another loopback address, e.g. `SERVER_IP=127.0.0.2`.

How it runs: the server has a git checkout; `deploy/deploy.sh` builds the image there (no registry), tags it
`<date-time>-<git sha>` and starts `db`, `migrate` (one-shot), `api`, `worker` (exactly one), `mock-helpdesk`
with `compose.yaml` + `deploy/compose.prod.yaml`. Only two ports listen, both on loopback: `api` on
`127.0.0.1:18030` and `mock-helpdesk` on `127.0.0.1:18031`; Postgres is not published. The shared Caddy
(container `caddy`, network `host`, config `/opt/caddy/Caddyfile` owned by root) proxies to them.

Ports (confirm they are free on the server: `ss -ltn`): loopback 18030/18031, temporary public 8030 (API, UI,
webhook) and 8031 (mock helpdesk page). Public and loopback ports differ on purpose: a Caddy `:8030` binds all
interfaces and would collide with a container on `127.0.0.1:8030`.

The api trusts `X-Forwarded-For` (uvicorn `--proxy-headers`) for its per-IP webhook rate limit and its logs.
That is safe only because 18030 is reachable from this host alone and Caddy replaces the client's header with
the real address. Never publish 18030 on `0.0.0.0`.

Steps marked **[sudo]** need root; everything else is a plain user command (in the docker group).

## First deploy

1. **Check Docker** (Compose v2.24+ is needed for `!reset`/`!override` in `compose.prod.yaml`):
   ```
   docker compose version
   ```
2. **Code.** The owner pushes the branch to GitHub first. Then on the server:
   ```
   git clone https://github.com/arturkhamit/test_task_momentum_squads.git "$DEPLOY_DIR"
   cd "$DEPLOY_DIR"
   git checkout main            # or feat/support-drafts until it is merged
   ```
3. **`.env`** (on the server; the passwords are generated there and never leave it):
   ```
   cd "$DEPLOY_DIR"
   (umask 077; sed \
     -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 32)/" \
     -e "s/^OPERATOR_PASSWORD=.*/OPERATOR_PASSWORD=$(openssl rand -hex 16)/" \
     -e "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=http://$SERVER_IP:8030|" \
     deploy/.env.prod.example > .env)
   chmod 600 .env
   grep -E '^OPERATOR_(USER|PASSWORD)=' .env     # the UI login; keep it in your password manager
   ```
   `ANTHROPIC_API_KEY` is optional. Empty = no-key mode: the worker still sends a real request (it gets 401),
   waits 5-30 s and writes a static draft with the KB links; statuses, UI and sending work as with the real
   path. To switch to real Claude drafts later, put the key in `.env` with an editor (not on the command line)
   and run `deploy/deploy.sh` again. `POSTGRES_PASSWORD` is hex on purpose (it goes into a URL) and only takes
   effect when the database volume is first created.
4. **Deploy:**
   ```
   bash deploy/deploy.sh
   ```
   Preflight (`.env` present, mode 600, no `change-me`, the required variables set, passwords long enough,
   `PUBLIC_BASE_URL` not localhost, 3+ GB free) -> build -> migrate + KB seed -> start, waiting up to 120 s for
   the healthchecks -> smoke of `/health`, `/ready` and the mock on the loopback ports -> `.last_good_tag` (the
   running release; the one it replaced goes to `.previous_tag`). The last lines print both.
   Until the next step the service answers on the server's loopback only.
5. **[sudo] Shared Caddy:** section below.
6. **[sudo] Firewall** for the temporary ports, if the server has one:
   ```
   sudo ufw status
   sudo ufw allow 8030/tcp
   sudo ufw allow 8031/tcp
   ```
   And the same in the provider's panel, if it has a firewall. (18030/18031 stay closed: they are loopback.)
7. **Check from the laptop** (section Check).

## Shared Caddy [sudo]

`deploy/momentum.caddy` holds three snippets and two site blocks. The mock page uses Basic Auth with the
operator's login; Caddy needs the password as a bcrypt hash, so the two `__MOCK_*__` placeholders are filled
while appending. `/opt/caddy/Caddyfile` is mounted into the container as a single file, so it must be edited
**in place** (same inode): an editor or `mv` that replaces it leaves the container on the old content.

```
cd "$DEPLOY_DIR"
ls -l /opt/caddy/Caddyfile                                    # owned by root: the lines below use sudo
sudo cp /opt/caddy/Caddyfile /root/Caddyfile.bak-$(date +%F)
MOCK_USER=$(grep -E '^OPERATOR_USER=' .env | cut -d= -f2-)
MOCK_PW=$(grep -E '^OPERATOR_PASSWORD=' .env | cut -d= -f2-)
# hash-password reads the password and its confirmation as lines; without newlines it fails with "EOF"
MOCK_HASH=$(printf '%s\n%s\n' "$MOCK_PW" "$MOCK_PW" | sudo docker exec -i caddy caddy hash-password); unset MOCK_PW
sed -e "s|__MOCK_USER__|$MOCK_USER|" -e "s|__MOCK_PASSWORD_HASH__|$MOCK_HASH|" deploy/momentum.caddy \
  | sudo tee -a /opt/caddy/Caddyfile > /dev/null             # at the end, nothing else
unset MOCK_USER MOCK_HASH
sudo docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo docker exec caddy caddy reload   --config /etc/caddy/Caddyfile --adapter caddyfile
```

The password goes to `hash-password` on stdin, so it never shows in the process list. Without the shared
container: `docker run --rm -i caddy:2 caddy hash-password` (type the password, then Ctrl-D).

If `validate` fails, put the backup back **in place** (`sudo sh -c 'cat /root/Caddyfile.bak-... > /opt/caddy/Caddyfile'`)
and reload. Do not `cp`/`mv` over the file. (Drop `sudo` before `docker` if your user is in the docker group.)
After a change of `OPERATOR_USER`/`OPERATOR_PASSWORD`, replace the `basic_auth` line of the mock block the same way.

What the blocks do: `encode zstd gzip`; request bodies over 64 KB get 413 (webhook bodies are small; a full UI
form is about 60 KB at most); `X-Frame-Options DENY`, `Content-Security-Policy: frame-ancestors 'none'`,
`X-Content-Type-Options nosniff`, `Referrer-Policy`, no `Server` header. The `Host` header passes through unchanged:
the UI's CSRF check compares `Origin` with `Host`, so never add `header_up Host`. The API access log is
`/data/momentum_access.log` inside the caddy container (10 MiB x 3; Caddy leaves `Authorization` out).

## Check

From the laptop, after the Caddy reload:

```
curl -fsS http://$SERVER_IP:8030/health                                          # OK
make smoke BASE_URL=http://$SERVER_IP:8030                                       # tests/smoke.py
uv run python tests/smoke.py --base-url http://$SERVER_IP:8030 --ui-user admin --ui-password '<operator password>'
curl -s -o /dev/null -w '%{http_code}\n' http://$SERVER_IP:8031/                 # 401 without the login
head -c 70000 /dev/zero | curl -s -o /dev/null -w '%{http_code}\n' -X POST --data-binary @- \
  -H 'Content-Type: application/json' http://$SERVER_IP:8030/webhooks/helpdesk   # 413 (body over 64 KB)
```

`tests/smoke.py` is read-only apart from one ticket marked `[smoke test]` (`--skip-webhook` avoids even that).
It becomes a draft in the UI: reject it. Then open `http://<server-ip>:8030/ui` (operator login) and
`http://<server-ip>:8031/` (same login) in a browser, and send the example ticket from the root README to
`http://<server-ip>:8030/webhooks/helpdesk` for an end-to-end run.

## Next deploys

On the server:

```
cd "$DEPLOY_DIR" && git pull --ff-only && bash deploy/deploy.sh        # or: make deploy
```

Before migrating it dumps the database to `backups/` (when it has tables). A failure after the build rolls back
to the release that was running (`.last_good_tag`) by itself; the first deploy has nothing to roll back to (`make prod-logs`).
The mock helpdesk keeps its replies in memory: every deploy empties its page (the tickets in Postgres stay).

## Rollback

```
bash deploy/rollback.sh                          # to the release before the current one (.previous_tag); or: make rollback
bash deploy/rollback.sh 20261106-120000-abc1234  # to a given tag (docker image ls momentum-squads lists them)
```

`.last_good_tag` is the running release, `.previous_tag` the one before it. A rollback swaps them, so running
`deploy/rollback.sh` again returns to the release you rolled back from. Without `.previous_tag` (only one
release so far) it stops with a message and changes nothing.

Only containers go back, and `migrate` is not run (the old alembic would not know the new revision). Migrations
are not undone: if the new version changed the schema and the old code cannot read it, restore the dump taken
just before the deploy (while the new release still runs), then roll back:

```
export COMPOSE_FILE=compose.yaml:deploy/compose.prod.yaml TAG=$(cat .last_good_tag)
docker compose stop api worker
docker compose exec -T db sh -c 'pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < backups/momentum-<time>.dump
bash deploy/rollback.sh
```

Never `docker compose down -v` or `docker volume rm`: the `pgdata` volume is the database. Old images stay on
the server; remove ones you no longer need with `docker image rm momentum-squads:<tag>` (not the one in
`.last_good_tag`).

## Backups

`deploy/backup.sh` (`make backup`): `pg_dump -Fc` into `backups/momentum-<time>.dump` (mode 600, checked with
`pg_restore -l`), newest 14 kept (`BACKUP_KEEP`). `deploy.sh` runs it before every migration. Daily as well,
optional (`crontab -e`):

```
15 3 * * * cd <deploy-dir> && deploy/backup.sh >> backups/cron.log 2>&1
```

The dumps hold customer messages: copy them off the server if you need them to survive a lost disk.

## Logs

```
make prod-ps                                    # health of every service
make prod-logs                                  # api, worker, mock-helpdesk; JSON lines, 10 MB x 3 per container
sudo docker exec caddy tail -n 50 /data/momentum_access.log
```

## Later: the domain with TLS [sudo]

The owner wants `<domain>` for the API, UI and webhook, and `helpdesk.<domain>` for the mock page.

1. DNS: A records of `<domain>` and `helpdesk.<domain>` -> the server. Ports 80 and 443 must be open.
2. In `/opt/caddy/Caddyfile` add the two commented domain blocks from `deploy/momentum.caddy` (uncommented,
   `your-domain.tld` replaced; in place, as above), validate, reload. Caddy gets the certificates itself. The
   snippets they import are already in the file.
3. `.env`: `PUBLIC_BASE_URL=https://<domain>`, then `bash deploy/deploy.sh` (settings are read at start).
4. Stored drafts keep their old links: the KB links are baked into `tickets.draft` and `citations` when a draft
   is generated, so drafts made before the switch point at `http://<server-ip>:8030/kb/...`. Replies already
   sent cannot change. Either keep the `:8030`/`:8031` blocks until those drafts are handled, or rewrite the
   pending ones:
   ```
   export COMPOSE_FILE=compose.yaml:deploy/compose.prod.yaml TAG=$(cat .last_good_tag)
   docker compose exec -T db psql -U app -d app -c "update tickets set
     draft = replace(draft, 'http://<server-ip>:8030/', 'https://<domain>/'),
     citations = replace(citations::text, 'http://<server-ip>:8030/', 'https://<domain>/')::jsonb
     where status = 'draft'"
   ```
5. Then remove the `:8030`/`:8031` blocks (in place), reload, and close the ports:
   `sudo ufw delete allow 8030/tcp; sudo ufw delete allow 8031/tcp`.
6. Optional, once HTTPS works: add `Strict-Transport-Security "max-age=31536000"` to the `header` block of
   the `(momentum_common)` snippet in `/opt/caddy/Caddyfile` (not before: the temporary blocks are plain http).
