import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, update

from app import sender, worker
from app.config import settings
from app.db import SessionLocal
from app.models import Ticket

REPLY_URL = "https://helpdesk.example.com/tickets/T-1/replies?source=ai"


async def add_approved(n=0, **values):
    row = {
        "event_id": f"send-{n}",
        "ticket_id": "T-1",
        "customer_email": "user@example.com",
        "message": "Не можу експортувати звіт у PDF",
        "reply_url": REPLY_URL,
        "status": "approved",
        "final_message": "Ось інструкція.",
    } | values
    async with SessionLocal() as s, s.begin():
        return await s.scalar(insert(Ticket).values(**row).returning(Ticket.id))


async def get_ticket(ticket_id):
    async with SessionLocal() as s:
        return await s.get(Ticket, ticket_id)


async def make_due(ticket_id):
    async with SessionLocal() as s, s.begin():
        await s.execute(
            update(Ticket).where(Ticket.id == ticket_id).values(next_attempt_at=func.now())
        )


def in_seconds(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


# --- target_url --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://helpdesk.example.com/tickets/1/replies",
        "file:///etc/passwd",
        "https://user:pass@helpdesk.example.com/tickets/1/replies",
        "https://helpdesk.example.com\\@evil.example/x",
        "https://evil.example/tickets/1/replies",
        "https://helpdesk.example.com.evil.example/x",
        "https://evil.example#@helpdesk.example.com",
        "http://169.254.169.254/latest/meta-data",
        "not a url",
    ],
)
def test_target_url_rejects_untrusted_urls(url):
    with pytest.raises(ValueError):
        sender.target_url(url)


def test_target_url_allowlist_is_exact_and_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, "helpdesk_allowed_hosts", "a.example, B.example")
    assert sender.target_url("https://A.EXAMPLE/x") == "https://a.example/x"
    assert sender.target_url("http://b.example:8080/x") == "http://b.example:8080/x"
    with pytest.raises(ValueError):
        sender.target_url("https://sub.a.example/x")


def test_target_url_mock_rewrite_keeps_path_and_query(monkeypatch):
    monkeypatch.setattr(settings, "helpdesk_mock_url", "http://mock-helpdesk:8001")
    assert (
        sender.target_url("https://helpdesk.example.com:8443/tickets/T-1/replies?a=1&b=2")
        == "http://mock-helpdesk:8001/tickets/T-1/replies?a=1&b=2"
    )


def test_target_url_without_mock_is_unchanged():
    assert sender.target_url(REPLY_URL) == REPLY_URL


# --- sending -----------------------------------------------------------------------------


async def test_happy_path_sends_once(helpdesk):
    ticket_id = await add_approved()
    assert await worker.send_once() is True
    assert await worker.send_once() is False

    t = await get_ticket(ticket_id)
    assert t.status == "sent" and t.sent_at is not None and t.locked_until is None
    [record] = helpdesk.records
    assert record["path_ticket_id"] == "T-1"
    assert record["ticket_id"] == "T-1" and record["message"] == "Ось інструкція."
    assert record["idempotency_key"] == str(ticket_id)


async def test_503_then_201_sends_exactly_one_reply(helpdesk):
    ticket_id = await add_approved()
    helpdesk.settings.fail_rate = 1.0
    await worker.send_once()
    t = await get_ticket(ticket_id)
    assert t.status == "approved" and t.attempts == 1 and t.last_error == "HTTP 503"
    assert in_seconds(2) < t.next_attempt_at < in_seconds(8)  # 5 s +-50%
    assert helpdesk.records == []

    helpdesk.settings.fail_rate = 0.0
    await make_due(ticket_id)
    await worker.send_once()
    t = await get_ticket(ticket_id)
    assert t.status == "sent" and t.attempts == 2 and t.last_error is None
    assert len(helpdesk.records) == 1
    assert dict(helpdesk.attempts) == {str(ticket_id): 2}  # same Idempotency-Key both times


async def test_slow_but_stored_reply_is_not_duplicated(helpdesk):
    ticket_id = await add_approved()
    helpdesk.settings.slow_rate = 1.0  # stores, then answers after 1 s; our timeout is 0.5 s
    await worker.send_once()
    t = await get_ticket(ticket_id)
    assert t.status == "approved" and t.last_error.startswith("timeout")
    assert len(helpdesk.records) == 1

    helpdesk.settings.slow_rate = 0.0
    await make_due(ticket_id)
    await worker.send_once()
    assert (await get_ticket(ticket_id)).status == "sent"
    assert len(helpdesk.records) == 1
    assert dict(helpdesk.attempts) == {str(ticket_id): 2}


async def test_client_error_fails_at_once(helpdesk):
    ticket_id = await add_approved(reply_url="https://helpdesk.example.com/no/such/endpoint")
    await worker.send_once()
    t = await get_ticket(ticket_id)
    assert t.status == "send_failed" and t.last_error == "HTTP 404" and t.attempts == 1


async def test_disallowed_host_fails_without_a_request(helpdesk):
    ticket_id = await add_approved(reply_url="https://evil.example/tickets/T-1/replies")
    await worker.send_once()
    t = await get_ticket(ticket_id)
    assert t.status == "send_failed" and "HELPDESK_ALLOWED_HOSTS" in t.last_error
    assert helpdesk.records == [] and not helpdesk.attempts


async def test_retries_stop_at_max_attempts(helpdesk, monkeypatch):
    monkeypatch.setattr(settings, "send_max_attempts", 3)
    helpdesk.settings.fail_rate = 1.0
    ticket_id = await add_approved()
    for _ in range(3):
        await make_due(ticket_id)
        await worker.send_once()
    t = await get_ticket(ticket_id)
    assert t.status == "send_failed" and t.attempts == 3
    assert dict(helpdesk.attempts) == {str(ticket_id): 3}


async def test_connection_error_is_retried(monkeypatch):
    monkeypatch.setattr(settings, "helpdesk_mock_url", "http://127.0.0.1:1")
    ticket_id = await add_approved()
    await worker.send_once()
    t = await get_ticket(ticket_id)
    assert t.status == "approved" and t.last_error.startswith("connection error")


async def test_expired_sending_lease_is_reclaimed(helpdesk):
    ticket_id = await add_approved(
        status="sending", attempts=1, locked_until=datetime.now(UTC) - timedelta(seconds=1)
    )
    assert await worker.send_once() is True
    t = await get_ticket(ticket_id)
    assert t.status == "sent" and t.attempts == 2


async def test_live_sending_lease_is_not_reclaimed(helpdesk):
    await add_approved(status="sending", attempts=1, locked_until=in_seconds(30))
    assert await worker.send_once() is False


async def test_ticket_that_keeps_crashing_the_sender_ends_failed(helpdesk):
    ticket_id = await add_approved(
        status="sending",
        attempts=settings.send_max_attempts,
        locked_until=datetime.now(UTC) - timedelta(seconds=1),
    )
    await worker.send_once()
    t = await get_ticket(ticket_id)
    assert t.status == "send_failed" and "lease expired" in t.last_error
    assert helpdesk.records == []


async def test_cancelled_send_is_handed_back(monkeypatch):
    started = asyncio.Event()

    async def hang(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sender, "post_reply", hang)
    ticket_id = await add_approved()
    task = asyncio.create_task(worker.send_once())
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    t = await get_ticket(ticket_id)
    assert t.status == "approved" and t.locked_until is None
    assert t.attempts == 0  # the interrupted attempt is given back
