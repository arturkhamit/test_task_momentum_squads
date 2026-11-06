# CLAUDE.md

Support draft service: a helpdesk webhook takes in tickets, a worker drafts replies with an LLM from a small knowledge base, an operator approves them in a web UI, and approved replies are posted back to the helpdesk.

**`SPEC.md` is the source of truth** for requirements (R1–R14), architecture and the delivery plan. Read it before changing anything. If code and spec disagree, raise it; do not silently pick one.

## Stack

Python 3.12, uv, FastAPI, SQLAlchemy 2 (async, asyncpg), Alembic, PostgreSQL 16, httpx, anthropic SDK, Jinja2, pytest, ruff. Everything runs with `docker compose up`.

## Commands

```bash
uv sync                          # install deps
docker compose up -d db          # Postgres for tests
uv run pytest                    # tests
uv run ruff check . && uv run ruff format --check .
docker compose up --build        # whole stack: api, worker, mock-helpdesk, db
```

## Invariants: do not break these

- **The webhook does only an insert.** `INSERT … ON CONFLICT (event_id) DO NOTHING`, then 200. No LLM calls, no HTTP calls, nothing slow: the helpdesk retries after 2 s.
- **Postgres is the queue.** Claim jobs with `FOR UPDATE SKIP LOCKED` plus a `locked_until` lease. No Redis or Celery.
- **Every LLM request goes through the global limiter** (60/min), including the no-key request and retries. SDK auto-retries stay off.
- **One worker replica.** The limiter is in-process.
- **Status changes are conditional updates** (`… WHERE status = '<expected>'`), so double clicks and races cannot repeat an action.
- **Every reply POST carries `Idempotency-Key: <tickets.id>`**, the same key on every retry.
- **`reply_url` is untrusted:** the host must be in `HELPDESK_ALLOWED_HOSTS`.
- **No-key mode must behave like the real path** apart from the answer text: a real request, a 5–30 s delay, the same limiter and the same statuses.
- **Every setting has a default that works under `docker compose up`.** No `.env` is required locally.

## Conventions

- English everywhere: code, comments, docs, commit messages. KB articles in `kb/` are Ukrainian on purpose.
- Work on branch `feat/support-drafts`. Make small commits, one logical step each: the commit history is part of the review. Never push, merge, rebase or squash.
- New non-trivial logic gets a test. Tests run against real Postgres, not SQLite.
- Secrets only via env; `.env` is git-ignored; keep `.env.example` complete.
- Keep it small: no abstractions, options or services the spec does not ask for.
