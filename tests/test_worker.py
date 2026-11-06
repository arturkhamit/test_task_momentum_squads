import asyncio
import json
from datetime import UTC, datetime, timedelta

from conftest import error_response, message_response
from sqlalchemy import func, insert, select, update

from app import llm, worker
from app.config import settings
from app.db import SessionLocal
from app.kb import article_url
from app.models import Ticket

PAYLOAD = {
    "event_id": "evt-w1",
    "ticket_id": "T-1",
    "customer_email": "user@example.com",
    "message": "Не можу експортувати звіт у PDF",
    "reply_url": "https://helpdesk.example.com/api/tickets/T-1/replies",
}


async def add_ticket(n: int = 0, **values) -> str:
    row = PAYLOAD | {"event_id": f"evt-{n}"} | values
    async with SessionLocal() as s, s.begin():
        return await s.scalar(insert(Ticket).values(**row).returning(Ticket.id))


async def get_ticket(ticket_id) -> Ticket:
    async with SessionLocal() as s:
        return await s.get(Ticket, ticket_id)


async def make_due(ticket_id) -> None:
    async with SessionLocal() as s, s.begin():
        await s.execute(
            update(Ticket).where(Ticket.id == ticket_id).values(next_attempt_at=func.now())
        )


def claim_for_generation():
    return worker.claim("new", "generating", settings.lease_seconds)


def ago(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


# --- claim -------------------------------------------------------------------------------


async def test_concurrent_claims_never_share_a_row():
    ids = {await add_ticket(n) for n in range(10)}
    jobs = await asyncio.gather(*(claim_for_generation() for _ in range(20)))
    claimed = [j["id"] for j in jobs if j is not None]
    assert sorted(claimed) == sorted(ids)
    for ticket_id in claimed:
        t = await get_ticket(ticket_id)
        assert t.status == "generating" and t.attempts == 1 and t.locked_until is not None


async def test_expired_lease_is_reclaimed():
    ticket_id = await add_ticket(status="generating", attempts=1, locked_until=ago(1))
    job = await claim_for_generation()
    assert job["id"] == ticket_id and job["attempts"] == 2
    assert (await get_ticket(ticket_id)).locked_until > datetime.now(UTC)


async def test_live_lease_and_future_retry_are_not_claimed():
    await add_ticket(1, status="generating", attempts=1, locked_until=ago(-60))
    await add_ticket(2, status="new", next_attempt_at=ago(-60))
    await add_ticket(3, status="draft")
    assert await claim_for_generation() is None


async def test_stale_worker_is_fenced_after_reclaim():
    """A worker whose lease expired and whose ticket was reclaimed cannot overwrite it."""
    ticket_id = await add_ticket(status="approved", final_message="Відповідь")
    stale = await worker.claim("approved", "sending", settings.send_lease_seconds)
    async with SessionLocal() as s, s.begin():
        await s.execute(update(Ticket).where(Ticket.id == ticket_id).values(locked_until=ago(1)))
    fresh = await worker.claim("approved", "sending", settings.send_lease_seconds)
    assert fresh["id"] == ticket_id and fresh["attempts"] == 2

    await worker.finish(stale, "sending", status="sent")

    t = await get_ticket(ticket_id)
    assert t.status == "sending" and t.attempts == 2 and t.sent_at is None


async def test_stale_worker_is_fenced_after_operator_retry():
    """Retry resets attempts to 0, so the attempts fence alone matches a pre-Retry worker
    again; the lease timestamp tells the two runs apart."""
    ticket_id = await add_ticket(status="approved", final_message="Відповідь")
    stale = await worker.claim("approved", "sending", settings.send_lease_seconds)
    # The stale run's lease expires, the ticket ends send_failed, the operator presses Retry.
    async with SessionLocal() as s, s.begin():
        await s.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id)
            .values(status="approved", attempts=0, locked_until=None)
        )
    fresh = await worker.claim("approved", "sending", settings.send_lease_seconds)
    assert fresh["attempts"] == stale["attempts"] == 1

    await worker.finish(stale, "sending", status="approved", last_error="stale")

    t = await get_ticket(ticket_id)
    assert t.status == "sending" and t.last_error is None


# --- generation --------------------------------------------------------------------------


async def test_happy_path_webhook_to_draft(client, anthropic_stub, caplog):
    pdf_url = article_url("export-pdf")
    requests = anthropic_stub(
        lambda r: message_response(f"Інструкція тут: {pdf_url}"), api_key="sk-test"
    )
    caplog.set_level("INFO", logger="app.llm")
    assert (await client.post("/webhooks/helpdesk", json=PAYLOAD)).status_code == 200

    assert await worker.generate_once() is True
    assert await worker.generate_once() is False

    async with SessionLocal() as s:
        t = await s.scalar(select(Ticket).where(Ticket.event_id == "evt-w1"))
    assert t.status == "draft"
    assert t.draft == f"Інструкція тут: {pdf_url}"
    assert t.citations == [
        {"slug": "export-pdf", "title": "Як експортувати звіт у PDF", "url": pdf_url}
    ]
    assert t.attempts == 1 and t.locked_until is None and t.last_error is None
    prompt = json.loads(requests[0].content)["messages"][0]["content"]
    assert "Як експортувати звіт у PDF" in prompt and pdf_url in prompt
    assert [r.ticket_id for r in caplog.records if getattr(r, "event", "") == "llm_request"] == [
        str(t.id)
    ]


async def test_no_key_flow_reaches_draft_with_all_retrieved_links(
    anthropic_stub, fake_sleep, limiter
):
    requests = anthropic_stub(lambda r: error_response(401, "authentication_error"))
    ticket_id = await add_ticket()

    assert await worker.generate_once() is True

    t = await get_ticket(ticket_id)
    assert t.status == "draft"
    assert [c["slug"] for c in t.citations] == ["export-pdf"]
    assert article_url("export-pdf") in t.draft
    assert len(requests) == 1 and limiter.calls == 1
    assert len(fake_sleep) == 1 and 5 <= fake_sleep[0] <= 30


async def test_retryable_failure_backs_off_then_fails(anthropic_stub, monkeypatch):
    requests = anthropic_stub(lambda r: error_response(529, "overloaded_error"), api_key="sk-test")
    ticket_id = await add_ticket()

    await worker.generate_once()
    t = await get_ticket(ticket_id)
    assert t.status == "new" and t.attempts == 1 and t.locked_until is None
    assert "529" in t.last_error
    # First retry waits BACKOFF_BASE +-50%.
    assert ago(-4) < t.next_attempt_at < ago(-16)

    for attempt in range(2, settings.gen_max_attempts + 1):
        await make_due(ticket_id)
        assert await worker.generate_once() is True
        t = await get_ticket(ticket_id)
        assert t.attempts == attempt
    assert t.status == "generation_failed"
    assert len(requests) == settings.gen_max_attempts


async def test_rate_limit_respects_retry_after(anthropic_stub):
    anthropic_stub(
        lambda r: error_response(429, "rate_limit_error", {"retry-after": "120"}),
        api_key="sk-test",
    )
    ticket_id = await add_ticket()
    await worker.generate_once()
    t = await get_ticket(ticket_id)
    assert t.status == "new"
    assert t.next_attempt_at >= ago(-119)


async def test_non_retryable_failure_fails_at_once(anthropic_stub):
    anthropic_stub(lambda r: error_response(400, "invalid_request_error"), api_key="sk-test")
    ticket_id = await add_ticket()
    await worker.generate_once()
    t = await get_ticket(ticket_id)
    assert t.status == "generation_failed" and t.attempts == 1
    assert "BadRequestError" in t.last_error


async def test_ticket_that_keeps_crashing_the_worker_ends_failed(anthropic_stub):
    requests = anthropic_stub(lambda r: message_response("unused"), api_key="sk-test")
    ticket_id = await add_ticket(
        status="generating", attempts=settings.gen_max_attempts, locked_until=ago(1)
    )
    await worker.generate_once()
    t = await get_ticket(ticket_id)
    assert t.status == "generation_failed" and "lease expired" in t.last_error
    assert requests == []


async def test_cancelled_job_is_handed_back(monkeypatch):
    started = asyncio.Event()

    async def hang(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(llm, "draft_reply", hang)
    ticket_id = await add_ticket()
    task = asyncio.create_task(worker.generate_once())
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    t = await get_ticket(ticket_id)
    assert t.status == "new" and t.locked_until is None and t.next_attempt_at <= ago(0)
    assert t.attempts == 0  # the interrupted attempt is given back


async def test_pool_loop_stops_promptly():
    stop = asyncio.Event()
    task = asyncio.create_task(worker.pool_loop(worker.generate_once, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)


async def test_pool_loop_backs_off_after_errors_and_resets(monkeypatch):
    stop = asyncio.Event()
    outcomes = iter([RuntimeError] * 7 + [True, RuntimeError, False])
    pauses = []

    async def once():
        outcome = next(outcomes)
        if outcome is RuntimeError:
            raise RuntimeError("db down")
        return outcome

    async def record_pause(_stop, seconds):
        pauses.append(seconds)
        if len(pauses) == 9:
            stop.set()

    monkeypatch.setattr(worker, "pause", record_pause)
    await worker.pool_loop(once, stop)
    assert pauses == [1, 2, 4, 8, 16, 30, 30, 1, settings.poll_interval]


async def test_pool_loop_wakes_from_error_backoff_on_stop():
    stop = asyncio.Event()

    async def failing():
        raise RuntimeError("db down")

    task = asyncio.create_task(worker.pool_loop(failing, stop))
    for _ in range(3):
        await asyncio.sleep(0)  # let it fail and enter its 1 s backoff
    stop.set()
    await asyncio.wait_for(task, timeout=0.5)
