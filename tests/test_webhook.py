import asyncio

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
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
