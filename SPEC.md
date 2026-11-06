# Support Draft Service: Spec and Delivery Plan

Status: approved by the owner on 2026-11-06. Source task: "Тестове завдання: AI-first Backend Engineer" (3–4 h).

## 1. Goal

A helpdesk sends new tickets to our webhook. The service finds relevant knowledge-base (KB) articles, asks an LLM for a draft reply with links to them, and shows drafts to an operator. On approval the reply is posted back through the helpdesk API.

## 2. Acceptance criteria

Each line is a requirement from the task, phrased so it can be checked.

| ID | Requirement | How it is verified |
|---|---|---|
| R1 | `POST /webhooks/helpdesk` answers in < 2 s (p99), including at peak | load script, p99 |
| R2 | The same `event_id` delivered N times (also concurrently) creates exactly one ticket | pytest: 20 concurrent posts → 1 row |
| R3 | No ticket is lost: 500 tickets in 10 min all reach `draft` | load script + DB count |
| R4 | LLM calls ≤ 60 per minute for the whole service | count calls in the worker log / provider stub |
| R5 | LLM latency 5–30 s is tolerated; peak 50 tickets/min is drained | load script, backlog returns to 0 |
| R6 | Works without an API key: still sends a real request to the provider, then uses a static answer after a 5–30 s delay, and the rest of the flow is identical | pytest with patched sleep + manual run |
| R7 | Approved reply is POSTed to `reply_url`; 5xx and timeouts are retried | pytest against the mock |
| R8 | The customer never gets the same reply twice (retries, double-click on approve, worker crash) | mock helpdesk records count = 1 per ticket |
| R9 | Operator UI: list of tickets with drafts, approve / reject | manual + pytest |
| R10 | `GET /health` → `200`, body `OK` | pytest |
| R11 | `docker compose up` on a clean clone starts everything, with no `.env` and no API key | dockerizer + qa check |
| R12 | README with detailed run instructions; agent config (`CLAUDE.md`, `.claude/`, this spec) committed; history not squashed | review |
| R13 | (very desirable) Public deployment with links to the webhook and the operator UI | smoke against prod |
| R14 | The draft is based on relevant KB articles and links to them; links open | pytest: the example ticket finds the PDF-export article; `/kb/{slug}` returns 200 |

## 3. Architecture

Docker Compose services, all from one image except Postgres:

| Service | Command | Role |
|---|---|---|
| `db` | postgres:16 | data + job queue |
| `migrate` | `alembic upgrade head && python -m app.seed_kb` | one-shot: schema + KB articles |
| `api` | uvicorn `app.main:app` | webhook, operator UI, `/kb/{slug}`, `/health`, `/ready` |
| `worker` | `python -m app.worker` | generation loop + sending loop |
| `mock-helpdesk` | uvicorn `mock_helpdesk.main:app` | fake helpdesk API with flaky behaviour |

No Redis, no Celery: Postgres is the queue.

### 3.1 Data model

`tickets`
- `id uuid pk`, `event_id text UNIQUE`, `ticket_id text`, `customer_email text`, `message text`, `reply_url text`
- `status text`: `new → generating → draft → approved → sending → sent`; also `rejected`, `generation_failed`, `send_failed`
- `draft text`, `citations jsonb` (list of `{slug, title, url}`), `final_message text` (what was approved, possibly edited)
- `attempts int`, `next_attempt_at timestamptz`, `locked_until timestamptz`, `last_error text`
- `created_at`, `updated_at`, `sent_at`

`kb_articles`: `slug pk`, `title`, `body`, plus a pg_trgm index on `title || ' ' || body`.

### 3.2 Flows and key decisions

**Intake (R1–R3).** Validate the body with pydantic (422 on bad input) and run one statement: `INSERT … ON CONFLICT (event_id) DO NOTHING`. Answer `200 {"status":"accepted"}` for new and duplicate events alike, and only after commit, so an acknowledged ticket is always durable. The request does no LLM work. The unique index handles concurrent duplicates, with no app-level locks.

**Job pickup (R3, crash safety).** The worker claims rows with `SELECT … WHERE status=… AND next_attempt_at<=now() AND (locked_until IS NULL OR locked_until<now()) FOR UPDATE SKIP LOCKED LIMIT n`, then sets a lease (`locked_until = now()+lease`). A worker killed mid-job leaves an expired lease, and the row is picked up again.

**Generation (R4–R6).**
- The worker is one asyncio process. Concurrency comes from a semaphore (~20) and a global token-bucket limiter at 60 requests/min. Every provider request goes through the limiter, including the no-key one and retries.
- Peak load is 50 tickets/min at ~17 s average latency, which needs ~15 in flight.
- Constraint: exactly one worker replica, because the limiter is in-process. This is documented. Moving the limiter into Postgres is the upgrade path.
- Provider: Anthropic Claude, model `claude-haiku-5-5`, official `anthropic` SDK, client timeout 60 s, SDK retries off (our retries go through the limiter). On 429/5xx/timeout: exponential backoff, at most 5 attempts, then `generation_failed`.
- No-key mode: when `ANTHROPIC_API_KEY` is empty, still send the real request with a dummy key and expect 401. Then sleep `uniform(5, 30)` and return a static draft template filled with the retrieved article links. From that point the flow is identical.
- Prompt: answer in the customer's language, only from the given articles, cite them as links, and say so shortly and plainly if no article fits. The output is plain text.

**KB (R14).** 10 different short AI-written articles in Ukrainian about a fictional reporting SaaS, stored as `kb/*.md` in the repo and seeded into `kb_articles`. Articles are in Ukrainian because tickets are: trigram search only matches text in the same language. Search uses pg_trgm `word_similarity` against the ticket message and takes the top 3 above a threshold. That is enough for 10 articles; embeddings would be overkill. Known limit: matching is lexical, so synonyms without a shared root are missed (noted in the README). Articles are served at `/kb/{slug}`, so citation links really open (`PUBLIC_BASE_URL` + `/kb/{slug}`).

**Moderation (R9).**
- One server-rendered Jinja page behind HTTP Basic Auth (`OPERATOR_USER` / `OPERATOR_PASSWORD`).
- The page lists tickets newest first with a status filter. Each draft is an editable textarea with Approve / Reject buttons; failed tickets get a Retry button.
- Approve is `UPDATE … SET status='approved', final_message=… WHERE id=… AND status='draft'`, so a double click or two operators cannot queue two sends.
- Reject is final.

**Sending (R7, R8).**
- The sending loop claims `approved` tickets with the same lease pattern and POSTs `{"ticket_id", "message"}` to `reply_url` with the header `Idempotency-Key: <tickets.id>` and a 10 s timeout.
- 201/200 → `sent`. 5xx, timeout or connection error → retry with backoff (up to ~10 attempts over ~30 min), then `send_failed`. Other 4xx → `send_failed` immediately.
- **Assumption, stated in the README:** after a timeout we cannot know whether the helpdesk stored the reply. Exactly-once delivery is only possible if the helpdesk deduplicates by an idempotency key. Our mock does; a real helpdesk must too.
- `reply_url` comes from outside (SSRF risk), so its host must be in `HELPDESK_ALLOWED_HOSTS`.
- When `HELPDESK_MOCK_URL` is set, the scheme and host of `reply_url` are replaced with the mock (the path is kept). The task's example payload with `helpdesk.example.com` then works end to end.

**Mock helpdesk.**
- `POST /tickets/{id}/replies`: deduplicates by `Idempotency-Key` (a known key returns 201 without a new record).
- With probability `MOCK_FAIL_RATE` it returns 503.
- With probability `MOCK_SLOW_RATE` it **stores the reply and then sleeps 15 s**, past our timeout. This is the "processed but the response was lost" case.
- `GET /` shows received replies, with any duplicates highlighted (there should be none). `GET /replies` returns JSON for tests.

**Other.**
- `GET /health` returns plain `OK`; `GET /ready` returns 503 when the DB is down.
- Logs are JSON to stdout, level from `LOG_LEVEL`.
- Config via `pydantic-settings`. Every variable has a default that works under `docker compose up` (R11); `.env.example` lists them all.

## 4. Repository layout

```
app/            main.py config.py db.py models.py webhook.py ui.py kb.py llm.py worker.py sender.py seed_kb.py templates/
mock_helpdesk/  main.py
kb/             *.md (KB articles)
migrations/     alembic
scripts/        fire.py (send N webhooks incl. concurrent duplicates, to BASE_URL)
tests/          pytest; tests/load/ (stress scripts)
deploy/         compose.prod.yaml, momentum.caddy, deploy.sh, rollback.sh, README.md
Dockerfile  compose.yaml  .env.example  Makefile  README.md  SPEC.md  CLAUDE.md  .claude/
```

Stack: Python 3.12, uv, FastAPI, SQLAlchemy 2 (async, asyncpg), Alembic, httpx, anthropic, Jinja2, pytest, ruff.

## 5. Delivery plan

All work goes on one branch, `feat/support-drafts`, made from `main`. Agents commit in small logical steps, because history is reviewed. Nobody pushes or merges.

| # | Who | Scope | Gate (orchestrator checks itself) |
|---|---|---|---|
| 0 | orchestrator | create the branch; commit `.gitignore`, `CLAUDE.md`, this spec | — |
| 1 | backend-fixer | skeleton (uv, settings, db, Alembic), `/health` `/ready`, webhook + dedup, KB articles + seed + search + `/kb/{slug}`, dev `compose.yaml` with `db` only for tests | pytest green incl. R2 concurrency test |
| 2 | backend-fixer (same agent, continued) | worker: lease/SKIP LOCKED, generation loop, LLM client, no-key mode, limiter, retries | tests: limiter, lease reclaim, no-key path |
| 3 | backend-fixer (same agent, continued) | operator UI, sending loop, Idempotency-Key, allowlist + mock rewrite, mock-helpdesk, `scripts/fire.py` | tests: double approve → 1 send; 503→201 → 1 record; slow-but-stored → 1 record |
| 4 | dockerizer | Dockerfile, full compose (db, migrate, api, worker, mock-helpdesk), works with no `.env`, README "run locally" | clean `docker compose up` + `scripts/fire.py` end to end |
| 5 | qa-tester | levels 1–7 incl. stress S1–S6 (below); ≤ 2 fix rounds via backend-fixer | verdict ✅/⚠️ |
| 6 | backend-auditor | fresh-eye review before the code goes public (replaces the initial audit, since there was no code) | P0 items fixed |
| 7 | deploy-engineer | `deploy/` for the owner's server, Caddy snippet, Makefile targets, README deploy section. Files only | `bash -n`, `compose config`, `caddy validate` |
| 8 | qa-tester | final: `deploy/`, Makefile, smoke through local Caddy, rollback | verdict |
| 9 | owner | buys the domain and points DNS; allows deploy; adds the Caddy block (sudo); puts `.env` on the server; merges to `main` without squash and pushes | qa-tester smoke against prod |

Stress scenarios for step 5:
- S1: 500 webhooks over 10 min (accelerated run allowed if noted) plus a 1-min burst.
- S2: 20 concurrent copies of one `event_id`.
- S3: mock helpdesk at 30% 503 and 10% slow-but-stored.
- S4: double approve.
- S5: `docker compose kill worker` mid-generation and mid-send.
- S6: LLM call rate ≤ 60/min under a full queue.

## 6. Out of scope

Webhook signature (not in the task; noted in the README), embeddings/pgvector, metrics/tracing, multiple worker replicas, operator accounts and roles, re-generation after reject, CI.

## 7. Owner decisions (2026-11-06)

1. **Domain:** the owner will buy one; it does not exist yet. Deploy files take it as a parameter (`DOMAIN`), so nothing is hard-coded. Public HTTPS through the shared Caddy on the owner's server.
2. **LLM key:** production uses a real `ANTHROPIC_API_KEY` from the server `.env`. Locally the default is no-key mode.
3. **Language:** everything about the project is in English: code, comments, docs, commit messages. KB articles stay in Ukrainian (see KB above).
4. **Exactly-once:** the `Idempotency-Key` approach and its documented assumption are accepted.
5. **Repository:** GitHub repo `arturkhamit/test_task_momentum_squads` already exists. Agents commit to `feat/support-drafts`; the owner merges to `main` (no squash) and pushes.
6. **Agents** run at effort `high`.
