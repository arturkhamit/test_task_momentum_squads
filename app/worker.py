"""Background worker: python -m app.worker

Postgres is the queue. Two pools of coroutines (generation: new -> generating -> draft;
sending: approved -> sending -> sent) each claim one ticket at a time with
FOR UPDATE SKIP LOCKED and a lease (locked_until), so a crashed worker's tickets are picked
up again once the lease expires.
"""

import asyncio
import logging
import random
import signal
from contextlib import suppress
from datetime import timedelta

from sqlalchemy import RowMapping, func, text, update

from app import kb, llm, sender
from app.config import settings
from app.db import SessionLocal, engine
from app.log import setup_logging
from app.models import Ticket

log = logging.getLogger("app.worker")  # not __main__ under python -m

GEN_BACKOFF = (10.0, 600.0)  # (base, cap) seconds; doubles per attempt, +-50% jitter
SEND_BACKOFF = (5.0, 300.0)

CLAIM_SQL = text("""
    UPDATE tickets
    SET status = :working,
        locked_until = now() + make_interval(secs => :lease),
        attempts = attempts + 1,
        updated_at = now()
    WHERE id = (
        SELECT id FROM tickets
        WHERE (status = :ready AND next_attempt_at <= now())
           OR (status = :working AND locked_until < now())
        ORDER BY next_attempt_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, ticket_id, message, reply_url, final_message, attempts
""")


async def claim(ready: str, working: str, lease: int) -> RowMapping | None:
    """Move one due `ready` ticket, or a `working` one whose lease expired (crashed worker),
    to `working` under a fresh lease."""
    async with SessionLocal() as session, session.begin():
        params = {"ready": ready, "working": working, "lease": lease}
        return (await session.execute(CLAIM_SQL, params)).mappings().first()


async def finish(job: RowMapping, working: str, **values) -> None:
    """Leave `working`. Matching attempts too means a job whose lease expired and was
    reclaimed by someone else can no longer be overwritten by us."""
    async with SessionLocal() as session, session.begin():
        await session.execute(
            update(Ticket)
            .where(
                Ticket.id == job["id"],
                Ticket.status == working,
                Ticket.attempts == job["attempts"],
            )
            .values(locked_until=None, updated_at=func.now(), **values)
        )


def backoff(attempts: int, base_cap: tuple[float, float], retry_after: float | None = None):
    base, cap = base_cap
    delay = min(base * 2 ** (attempts - 1), cap) * random.uniform(0.5, 1.5)
    return max(delay, retry_after or 0)


async def generate(job: RowMapping) -> None:
    ticket_id = str(job["id"])
    fields = {"ticket_id": ticket_id, "attempts": job["attempts"]}
    try:
        if job["attempts"] > settings.gen_max_attempts:
            raise llm.LLMError("lease expired too many times", retryable=False)
        articles = await kb.search(job["message"])
        draft = await llm.draft_reply(ticket_id, job["message"], articles)
    except asyncio.CancelledError:
        # Shutdown: hand the ticket back now instead of after the lease expires, and give the
        # attempt back so deploys do not use up a ticket's attempts.
        await finish(
            job,
            "generating",
            status="new",
            attempts=Ticket.attempts - 1,
            next_attempt_at=func.now(),
            last_error="worker stopped",
        )
        raise
    except Exception as e:
        if not isinstance(e, llm.LLMError):
            log.exception("unexpected generation error", extra={"event": "gen_error", **fields})
            e = llm.LLMError(f"{type(e).__name__}: {e}", retryable=True)
        if e.retryable and job["attempts"] < settings.gen_max_attempts:
            delay = backoff(job["attempts"], GEN_BACKOFF, e.retry_after)
            await finish(
                job,
                "generating",
                status="new",
                next_attempt_at=func.now() + timedelta(seconds=delay),
                last_error=str(e),
            )
            log.warning(
                "generation failed, will retry",
                extra={"event": "gen_retry", "error": str(e), "delay": round(delay, 1), **fields},
            )
        else:
            await finish(job, "generating", status="generation_failed", last_error=str(e))
            log.error("generation failed", extra={"event": "gen_failed", "error": str(e), **fields})
        return

    citations = [
        {"slug": a["slug"], "title": a["title"], "url": a["url"]}
        for a in articles
        if a["url"] in draft
    ]
    await finish(
        job, "generating", status="draft", draft=draft, citations=citations, last_error=None
    )
    log.info("draft ready", extra={"event": "draft_ready", "citations": len(citations), **fields})


async def send(job: RowMapping) -> None:
    ticket_id = str(job["id"])
    fields = {"ticket_id": ticket_id, "attempts": job["attempts"]}
    try:
        if job["attempts"] > settings.send_max_attempts:
            raise ValueError("lease expired too many times")
        url = sender.target_url(job["reply_url"])
    except ValueError as e:
        await finish(job, "sending", status="send_failed", last_error=str(e))
        log.error("reply not sent", extra={"event": "reply_failed", "error": str(e), **fields})
        return
    try:
        # The same key on every attempt lets the helpdesk drop repeats of a reply it stored.
        await sender.post_reply(url, ticket_id, job["ticket_id"], job["final_message"])
    except asyncio.CancelledError:
        # Shutdown: hand the ticket back now and give the attempt back. The POST may already
        # have landed; re-sending is safe because it carries the same Idempotency-Key.
        await finish(
            job,
            "sending",
            status="approved",
            attempts=Ticket.attempts - 1,
            next_attempt_at=func.now(),
            last_error="worker stopped",
        )
        raise
    except sender.SendError as e:
        if e.retryable and job["attempts"] < settings.send_max_attempts:
            delay = backoff(job["attempts"], SEND_BACKOFF)
            await finish(
                job,
                "sending",
                status="approved",
                next_attempt_at=func.now() + timedelta(seconds=delay),
                last_error=str(e),
            )
            log.warning(
                "reply not sent, will retry",
                extra={"event": "reply_retry", "error": str(e), "delay": round(delay, 1), **fields},
            )
        else:
            await finish(job, "sending", status="send_failed", last_error=str(e))
            log.error("reply not sent", extra={"event": "reply_failed", "error": str(e), **fields})
        return
    await finish(job, "sending", status="sent", sent_at=func.now(), last_error=None)
    log.info("reply sent", extra={"event": "reply_sent", **fields})


async def generate_once() -> bool:
    """Claim and process one ticket for generation; False when nothing was due."""
    job = await claim("new", "generating", settings.lease_seconds)
    if job is not None:
        await generate(job)
    return job is not None


async def send_once() -> bool:
    """Claim and send one approved reply; False when nothing was due."""
    job = await claim("approved", "sending", settings.send_lease_seconds)
    if job is not None:
        await send(job)
    return job is not None


async def pause(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake up at once when stop is set."""
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def pool_loop(once, stop: asyncio.Event) -> None:
    errors = 0  # consecutive failed iterations, e.g. while the DB is down
    while not stop.is_set():
        try:
            busy = await once()
            errors = 0
        except Exception:
            errors += 1
            busy = False
            log.exception(
                "worker loop error", extra={"event": "worker_error", "consecutive": errors}
            )
        if not busy:
            # Back off 1, 2, 4 ... 30 s after errors so an outage does not flood the log.
            await pause(stop, min(2 ** (errors - 1), 30) if errors else settings.poll_interval)


async def main() -> None:
    setup_logging()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(pool_loop(generate_once, stop)) for _ in range(settings.llm_concurrency)
    ] + [asyncio.create_task(pool_loop(send_once, stop)) for _ in range(settings.send_concurrency)]
    log.info(
        "worker started",
        extra={
            "event": "worker_started",
            "generation_concurrency": settings.llm_concurrency,
            "send_concurrency": settings.send_concurrency,
            "no_key": not settings.anthropic_api_key,
        },
    )

    await stop.wait()
    log.info("stopping: no new claims", extra={"event": "worker_stopping"})
    _, pending = await asyncio.wait(tasks, timeout=settings.shutdown_grace)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await engine.dispose()
    log.info("worker stopped", extra={"event": "worker_stopped", "cancelled": len(pending)})


if __name__ == "__main__":
    asyncio.run(main())
