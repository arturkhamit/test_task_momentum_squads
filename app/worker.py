"""Background worker: python -m app.worker

Postgres is the queue. Each of LLM_CONCURRENCY coroutines claims one ticket at a time with
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

from app import kb, llm
from app.config import settings
from app.db import SessionLocal, engine
from app.log import setup_logging
from app.models import Ticket

log = logging.getLogger("app.worker")  # not __main__ under python -m

BACKOFF_BASE = 10.0  # seconds before the 2nd attempt; doubles per attempt, +-50% jitter
BACKOFF_CAP = 600.0

CLAIM_SQL = text("""
    UPDATE tickets
    SET status = 'generating',
        locked_until = now() + make_interval(secs => :lease),
        attempts = attempts + 1,
        updated_at = now()
    WHERE id = (
        SELECT id FROM tickets
        WHERE (status = 'new' AND next_attempt_at <= now())
           OR (status = 'generating' AND locked_until < now())
        ORDER BY next_attempt_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, message, attempts
""")


async def claim() -> RowMapping | None:
    """Claim one due ticket, or one whose generating lease expired (crashed worker)."""
    async with SessionLocal() as session, session.begin():
        result = await session.execute(CLAIM_SQL, {"lease": settings.lease_seconds})
        return result.mappings().first()


async def finish(job: RowMapping, **values) -> None:
    """Leave 'generating'. Matching attempts too means a job whose lease expired and was
    reclaimed by someone else can no longer be overwritten by us."""
    async with SessionLocal() as session, session.begin():
        await session.execute(
            update(Ticket)
            .where(
                Ticket.id == job["id"],
                Ticket.status == "generating",
                Ticket.attempts == job["attempts"],
            )
            .values(locked_until=None, updated_at=func.now(), **values)
        )


def backoff(attempts: int, retry_after: float | None = None) -> float:
    delay = min(BACKOFF_BASE * 2 ** (attempts - 1), BACKOFF_CAP) * random.uniform(0.5, 1.5)
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
        # Shutdown: hand the ticket back now instead of after the lease expires.
        await finish(job, status="new", next_attempt_at=func.now(), last_error="worker stopped")
        raise
    except Exception as e:
        if not isinstance(e, llm.LLMError):
            log.exception("unexpected generation error", extra={"event": "gen_error", **fields})
            e = llm.LLMError(f"{type(e).__name__}: {e}", retryable=True)
        if e.retryable and job["attempts"] < settings.gen_max_attempts:
            delay = backoff(job["attempts"], e.retry_after)
            await finish(
                job,
                status="new",
                next_attempt_at=func.now() + timedelta(seconds=delay),
                last_error=str(e),
            )
            log.warning(
                "generation failed, will retry",
                extra={"event": "gen_retry", "error": str(e), "delay": round(delay, 1), **fields},
            )
        else:
            await finish(job, status="generation_failed", last_error=str(e))
            log.error("generation failed", extra={"event": "gen_failed", "error": str(e), **fields})
        return

    citations = [
        {"slug": a["slug"], "title": a["title"], "url": a["url"]}
        for a in articles
        if a["url"] in draft
    ]
    await finish(job, status="draft", draft=draft, citations=citations, last_error=None)
    log.info("draft ready", extra={"event": "draft_ready", "citations": len(citations), **fields})


async def run_once() -> bool:
    """Claim and process one ticket; False when nothing was due."""
    job = await claim()
    if job is None:
        return False
    await generate(job)
    return True


async def generation_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            if await run_once():
                continue
        except Exception:
            log.exception("generation loop error", extra={"event": "worker_error"})
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), settings.poll_interval)


async def main() -> None:
    setup_logging()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    tasks = [asyncio.create_task(generation_loop(stop)) for _ in range(settings.llm_concurrency)]
    # Pass 3 adds the sending pool here, next to the generation pool.
    log.info(
        "worker started",
        extra={
            "event": "worker_started",
            "generation_concurrency": settings.llm_concurrency,
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
