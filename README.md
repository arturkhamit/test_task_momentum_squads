# Support Draft Service

A helpdesk sends new tickets to a webhook. A background worker finds relevant knowledge-base (KB)
articles, asks an LLM (Anthropic Claude `claude-haiku-5-5`) for a draft reply with links to them,
and an operator approves or rejects drafts in a small web UI. Approved replies are posted back to
the helpdesk exactly once. It runs with a single `docker compose up`, with no `.env` and no API key
(a built-in no-key mode stands in for the LLM).

```
helpdesk --POST /webhooks/helpdesk--> api --INSERT--> Postgres (tickets = the queue)
                                                          |  SKIP LOCKED + lease
                                                          v
KB articles (pg_trgm) --> worker: LLM draft (<= 60 req/min) --> status "draft"
                                                          |
operator UI /ui (Basic Auth): edit, approve / reject <----+
                                                          |  status "approved"
                                                          v
        worker: POST reply_url, Idempotency-Key --> helpdesk (here: mock-helpdesk)
```

Services (one image for all but Postgres): `db`, `migrate` (one-shot: schema + KB seed), `api`,
`worker`, `mock-helpdesk`.

## Quick start

Requires Docker with Compose v2.24+ (for the optional `.env`). Nothing else.

```bash
docker compose up --build
```

| What | URL |
|---|---|
| Operator UI (Basic Auth `admin` / `admin`) | http://localhost:8000/ui |
| Webhook endpoint | `POST http://localhost:8000/webhooks/helpdesk` |
| Health | http://localhost:8000/health (plain `OK`) |
| Mock helpdesk (received replies) | http://localhost:8001 |

Send the example ticket from the task:

```bash
curl -X POST http://localhost:8000/webhooks/helpdesk \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "evt_8f2a",
    "ticket_id": "T-1042",
    "customer_email": "olena@example.com",
    "message": "Не можу експортувати звіт у PDF",
    "reply_url": "https://helpdesk.example.com/tickets/T-1042/replies"
  }'
# {"status":"accepted"}
```

What to expect:

1. The webhook answers `200 {"status":"accepted"}` at once. Sending the same `event_id` again
   answers the same and creates nothing new.
2. In no-key mode the worker makes a real request to Anthropic (it fails with 401, on purpose) and
   waits 5-30 s, then writes a static draft that links the relevant KB articles. In a test run the
   example ticket became a draft after about 23 s. Reload http://localhost:8000/ui.
3. Click **Approve** (the text is editable first). The ticket goes `approved` -> `sending` -> `sent`
   within a few seconds.
4. http://localhost:8001 now lists the reply. The example `reply_url` points at
   `helpdesk.example.com`; the stack rewrites its host to the mock (see
   [Sending](#how-it-works--design-decisions)).

The mock helpdesk is flaky on purpose (20 % of requests answer 503, 10 % store the reply and then
stall past the sender's timeout), so you may see retries; the page still shows one record per
ticket.

Logs are JSON lines on stdout:

```bash
docker compose logs -f api worker          # follow
docker compose logs --tail 50 worker
docker compose ps                          # health of every service
docker compose down                        # stop; the Postgres volume is kept
```

## Real LLM

```bash
cp .env.example .env
# set ANTHROPIC_API_KEY=sk-ant-... in .env
docker compose up -d worker    # recreates the worker with the new env
```

Only the worker uses the key. The draft is then written by Claude from the retrieved articles, in
the customer's language, with links to them.

## Load and duplicate demo

`scripts/fire.py` sends N tickets at a given rate, each as D concurrent copies of the same
`event_id`, and prints status codes and latency percentiles. There should be exactly one ticket
per `event_id` afterwards.

```bash
# from the host
uv run python scripts/fire.py --n 50 --rate 5 --dup 3 --base-url http://localhost:8000

# inside Docker, no host Python needed
docker compose run --rm api python scripts/fire.py --base-url http://api:8000 --n 10 --rate 5 --dup 3
```

Check the result in Postgres:

```bash
docker compose exec db psql -U app -d app -c "select status, count(*) from tickets group by 1"
```

In a test run, `--n 10 --dup 3` returned 30 x `200` with p99 about 27 ms and created 10 tickets.
Drafts then appear at about the LLM limit (60 per minute), so a big burst drains over time; the
UI list fills as they finish.

## Configuration

Every variable has a default that works under `docker compose up`. To change one, put it in `.env`
(copy `.env.example`) or export it in the shell. `compose.yaml` passes them to the containers.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://app:app@db:5432/app` | SQLAlchemy async URL |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Base for KB links in drafts (`/kb/{slug}`) |
| `LOG_LEVEL` | `INFO` | Log level |
| `OPERATOR_USER` / `OPERATOR_PASSWORD` | `admin` / `admin` | Basic Auth for `/ui`. Change both in production |
| `ANTHROPIC_API_KEY` | empty | Empty = no-key mode |
| `LLM_MODEL` | `claude-haiku-5-5` | Model name |
| `LLM_RPM` | `60` | LLM request starts per minute for the whole service |
| `LLM_TIMEOUT` | `60` | Seconds per LLM request |
| `STUB_DELAY_MIN` / `STUB_DELAY_MAX` | `5` / `30` | No-key mode delay range, seconds |
| `LLM_CONCURRENCY` | `20` | Tickets generated in parallel |
| `GEN_MAX_ATTEMPTS` | `5` | Generation attempts before `generation_failed` |
| `LEASE_SECONDS` | `300` | Generation lease; a crashed worker's job is retaken after it |
| `POLL_INTERVAL` | `1` | Queue poll interval, seconds |
| `SHUTDOWN_GRACE` | `8` | Seconds the worker waits for running jobs on SIGTERM (compose stop grace is 12 s) |
| `SEND_CONCURRENCY` | `5` | Replies sent in parallel |
| `SEND_TIMEOUT` | `10` | Seconds per reply POST |
| `SEND_LEASE_SECONDS` | `60` | Send lease; must exceed `SEND_TIMEOUT` |
| `SEND_MAX_ATTEMPTS` | `10` | Send attempts before `send_failed` |
| `HELPDESK_ALLOWED_HOSTS` | `helpdesk.example.com` | Comma-separated allowlist for the `reply_url` host |
| `HELPDESK_MOCK_URL` | `http://mock-helpdesk:8001` in compose (empty in the app) | If set, replaces the scheme and host of `reply_url` |
| `MOCK_FAIL_RATE` | `0.2` | Mock helpdesk: share of requests answered 503 without storing |
| `MOCK_SLOW_RATE` | `0.1` | Mock helpdesk: share stored, then answered late |
| `MOCK_SLOW_SECONDS` | `15` | Mock helpdesk: that delay |

Fixed in `compose.yaml`: the Postgres credentials (`app`/`app`/`app`, reachable only on
`127.0.0.1:55432`), the ports, the worker's single replica.

## How it works / design decisions

**Intake.** The webhook validates the body (422 on bad input) and runs one statement,
`INSERT ... ON CONFLICT (event_id) DO NOTHING`, then answers `200` only after commit, for new and
duplicate events alike. No LLM or HTTP calls in the request, so it answers in milliseconds even
when the LLM is slow, and an acknowledged ticket is always durable. The unique index is the whole
deduplication, including for concurrent copies: no app-level locks.

**Postgres as the queue.** The insert is the enqueue, and it is atomic with the ticket itself, so
there is no "saved but not queued" window and nothing to lose in a broker. The worker claims rows
with `SELECT ... FOR UPDATE SKIP LOCKED` and sets a lease (`locked_until`). If the worker dies
mid-job the lease expires and the row is picked up again. At roughly 1 ticket per second against an
LLM limit of 1 request per second, a table poll is plenty, so Redis and Celery would add moving
parts without helping.

**LLM rate limit and one worker.** Every provider request, including the no-key one and retries,
goes through a limiter that spaces request starts at least 1 s apart, so no 60-second window
holds more than 60. About 20 tickets are in flight at once (5-30
s latency at 50 tickets/min needs roughly 15). Retries (429, 5xx, timeout) use exponential backoff
with jitter, at most 5 attempts, then `generation_failed`; the SDK's own retries are off so nothing
bypasses the limiter. The limiter is in process memory, so **exactly one worker replica** is
supported. The upgrade path is moving the bucket into Postgres.

**No-key mode.** With an empty `ANTHROPIC_API_KEY` the worker still sends a real request with a
dummy key (it gets 401), waits a random 5-30 s and fills a static template with the retrieved
article links. Everything after that is identical to the real path: the same limiter, statuses and
UI.

**KB search.** 10 short Ukrainian articles in `kb/*.md`, seeded into `kb_articles` by the `migrate`
service. For a ticket, each distinct word (3+ letters, not a stop word) is fuzzy-matched against
articles with pg_trgm; every match adds its similarity weighted by IDF (words found in few articles
count more), doubled when the word is also in the article title. The top 3 above a threshold are
used, and nothing is returned if even the best is weak. Why not embeddings: for 10 articles
they would add a model, a vector store and a key for little gain. Known limit: matching is lexical,
so a synonym with no shared root is missed. Articles are served at `/kb/{slug}`, so the links in
drafts open.

**Sending and the exactly-once assumption.** The worker POSTs `{"ticket_id", "message"}` to
`reply_url` with `Idempotency-Key: <ticket uuid>`, the same key on every retry. 2xx means `sent`;
5xx, timeouts and connection errors are retried with backoff (up to 10 attempts); other 4xx fail at
once as `send_failed`. A timeout is ambiguous: we cannot know whether the helpdesk stored the
reply. Exactly-once delivery therefore only holds **if the helpdesk deduplicates by the idempotency
key**. The mock does (a known key returns 201 without a new record), and a real helpdesk must too.
Approve itself is a conditional update (`WHERE status='draft'`), so a double click or two operators
cannot queue two sends.

**`reply_url` allowlist (SSRF).** `reply_url` comes from outside, so its host must be in
`HELPDESK_ALLOWED_HOSTS`; credentials in the URL, non-http(s) schemes and redirects are refused.
The check runs on the original URL, then, if `HELPDESK_MOCK_URL` is set, the scheme and host are
replaced by the mock's (the path is kept). That is how the task's `helpdesk.example.com` payload
works end to end locally. In production, leave `HELPDESK_MOCK_URL` empty.

**Operator UI.** One server-rendered page behind HTTP Basic Auth, tickets newest first with a
status filter; drafts are editable; failed tickets have a Retry button. State-changing POSTs also
require an `Origin` (or `Referer`) whose host equals the request's `Host` header (CSRF defence for
Basic Auth). Behind a reverse proxy, pass the original `Host` through (Caddy does by default).

**Status lifecycle.**

```
new -> generating -> draft -> approved -> sending -> sent
         |             |                    |
         v             v                    v
 generation_failed   rejected          send_failed
 (Retry -> new)      (final)           (Retry -> approved)
```

Every transition is a conditional `UPDATE ... WHERE status = <expected>`.

**Container layout.** One non-root image (Python 3.12 slim, dependencies from `uv.lock`) runs the
four roles; compose supplies the command and the health check of each. `api` waits for `migrate`
to finish, `worker` too. The mock helpdesk runs as a single process because its store is in memory.

## Tests

Tests run against real Postgres (the compose `db` service, published on `127.0.0.1:55432`).

```bash
docker compose up -d db
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Known limitations / out of scope

- One worker replica: the LLM limiter is in process. Scaling out needs a shared limiter.
- The mock helpdesk keeps replies in memory (lost on restart) and must stay a single process.
- KB search is lexical (trigrams): synonyms without a shared root are missed. No embeddings.
- The webhook has no signature check (the task defines none). Anyone who can reach the URL can
  create tickets; in production restrict it at the proxy or add a shared-secret HMAC.
- Basic Auth with one shared operator account: no roles or accounts.
- A rejected ticket is final: no re-generation.
- No metrics or tracing; logs only.
- No CI.
- Exactly-once delivery depends on the helpdesk honouring `Idempotency-Key` (see above).

## How this was built

TODO(orchestrator)
