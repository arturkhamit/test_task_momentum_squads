# Daily commands. Local stack: compose.yaml. Production (on the server): deploy/*.sh, see deploy/README.md.
BASE_URL ?= http://localhost:8000
PROD = COMPOSE_FILE=compose.yaml:deploy/compose.prod.yaml TAG=$$(cat .last_good_tag) docker compose

.PHONY: up down logs ps test lint fire smoke deploy rollback backup prod-ps prod-logs

up:        ## whole stack, rebuilt
	docker compose up -d --build
down:      ## stop; the Postgres volume is kept (never -v)
	docker compose down
logs:
	docker compose logs -f --tail=100 api worker
ps:
	docker compose ps
test:      ## needs the db service
	docker compose up -d --wait db
	uv run pytest -q
lint:
	uv run ruff check . && uv run ruff format --check .
fire:      ## make fire BASE_URL=...
	uv run python scripts/fire.py --n 50 --rate 5 --dup 3 --base-url $(BASE_URL)
smoke:     ## make smoke BASE_URL=http://<server-ip>:8030
	uv run python tests/smoke.py --base-url $(BASE_URL)

# On the server only.
deploy:
	bash deploy/deploy.sh
rollback:  ## to .previous_tag, or make rollback tag=...
	bash deploy/rollback.sh $(tag)
backup:
	bash deploy/backup.sh
prod-ps:
	$(PROD) ps
prod-logs:
	$(PROD) logs -f --tail=100 api worker mock-helpdesk
