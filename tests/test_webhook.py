import asyncio
import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import SessionLocal, get_session
from app.main import app
from app.models import Ticket

PAYLOAD = {
    "event_id": "evt-1",
    "ticket_id": "T-1001",
    "customer_email": "user@example.com",
    "message": "Не можу експортувати звіт у PDF",
    "reply_url": "https://helpdesk.example.com/api/tickets/T-1001/replies",
}


async def count_tickets(event_id: str = "evt-1") -> int:
    async with SessionLocal() as s:
        return await s.scalar(select(func.count()).where(Ticket.event_id == event_id))


async def test_accepts_new_ticket(client):
    r = await client.post("/webhooks/helpdesk", json=PAYLOAD)
    assert r.status_code == 200
    assert r.json() == {"status": "accepted"}
    async with SessionLocal() as s:
        t = await s.scalar(select(Ticket).where(Ticket.event_id == "evt-1"))
    assert t.status == "new"
    assert t.ticket_id == "T-1001"
    assert t.reply_url == PAYLOAD["reply_url"]
    assert t.attempts == 0 and t.next_attempt_at is not None


@pytest.mark.parametrize(
    "patch",
    [
        {"event_id": ""},
        {"message": ""},
        {"message": "x" * 10_001},
        {"customer_email": "not-an-email"},
        {"reply_url": "ftp://helpdesk.example.com/x"},
        {"reply_url": "not a url"},
        {"ticket_id": None},
    ],
)
async def test_rejects_invalid_body(client, patch):
    r = await client.post("/webhooks/helpdesk", json=PAYLOAD | patch)
    assert r.status_code == 422
    assert await count_tickets() == 0


async def test_rejects_missing_field(client):
    body = {k: v for k, v in PAYLOAD.items() if k != "reply_url"}
    r = await client.post("/webhooks/helpdesk", json=body)
    assert r.status_code == 422


async def test_numeric_ids_are_accepted(client):
    r = await client.post("/webhooks/helpdesk", json=PAYLOAD | {"event_id": 42, "ticket_id": 7})
    assert r.status_code == 200
    assert await count_tickets("42") == 1


async def test_duplicate_event_sequential_creates_one_row(client):
    for _ in range(3):
        r = await client.post("/webhooks/helpdesk", json=PAYLOAD)
        assert r.status_code == 200
        assert r.json() == {"status": "accepted"}
    assert await count_tickets() == 1


async def test_duplicate_event_concurrent_creates_one_row(client):
    """R2: 20 concurrent deliveries of one event_id -> exactly one ticket, all acknowledged."""
    responses = await asyncio.gather(
        *(client.post("/webhooks/helpdesk", json=PAYLOAD) for _ in range(20))
    )
    assert [r.status_code for r in responses] == [200] * 20
    assert all(r.json() == {"status": "accepted"} for r in responses)
    assert await count_tickets() == 1


async def test_dead_db_answers_503_fast(client):
    # Nothing listens on port 1, so the connection is refused immediately.
    bad = create_async_engine("postgresql+asyncpg://app:app@127.0.0.1:1/app")

    async def dead_session():
        async with async_sessionmaker(bad)() as session:
            yield session

    app.dependency_overrides[get_session] = dead_session
    try:
        started = time.monotonic()
        r = await client.post("/webhooks/helpdesk", json=PAYLOAD)
    finally:
        app.dependency_overrides.clear()
        await bad.dispose()
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert r.json() == {"detail": "Database unavailable, retry later"}
    assert time.monotonic() - started < 1


async def test_hung_db_answers_503_within_timeout(client, monkeypatch, caplog):
    monkeypatch.setattr(settings, "webhook_db_timeout", 0.05)

    class HungSession:
        async def execute(self, *args, **kwargs):
            await asyncio.sleep(3600)

    async def hung_session():
        yield HungSession()

    app.dependency_overrides[get_session] = hung_session
    try:
        started = time.monotonic()
        r = await client.post("/webhooks/helpdesk", json=PAYLOAD)
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert time.monotonic() - started < 0.5
    [line] = [x for x in caplog.records if getattr(x, "event", "") == "webhook_db_error"]
    assert line.error == "TimeoutError" and line.exc_info is None
