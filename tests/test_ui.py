import asyncio

import pytest
from sqlalchemy import insert

from app.db import SessionLocal
from app.kb import article_url
from app.models import Ticket

AUTH = ("admin", "admin")
SAME_ORIGIN = {"Origin": "http://test"}
PDF = {
    "slug": "export-pdf",
    "title": "Як експортувати звіт у PDF",
    "url": article_url("export-pdf"),
}


async def add_ticket(n=0, **values):
    row = {
        "event_id": f"ui-{n}",
        "ticket_id": f"T-{n}",
        "customer_email": "user@example.com",
        "message": "Не можу експортувати звіт у PDF",
        "reply_url": "https://helpdesk.example.com/tickets/1/replies",
        "status": "draft",
        "draft": "Чернетка відповіді",
        "citations": [PDF],
    } | values
    async with SessionLocal() as s, s.begin():
        return await s.scalar(insert(Ticket).values(**row).returning(Ticket.id))


async def get_ticket(ticket_id):
    async with SessionLocal() as s:
        return await s.get(Ticket, ticket_id)


def post(client, path, data=None, headers=SAME_ORIGIN, auth=AUTH):
    return client.post(path, data=data or {}, headers=headers, auth=auth)


@pytest.mark.parametrize("auth", [None, ("admin", "wrong"), ("other", "admin")])
async def test_ui_requires_valid_basic_auth(client, auth):
    r = await client.get("/ui", auth=auth)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


async def test_root_redirects_to_ui(client):
    r = await client.get("/", auth=AUTH)
    assert r.status_code == 303 and r.headers["location"] == "/ui"


async def test_list_shows_draft_with_citations(client):
    await add_ticket()
    r = await client.get("/ui", auth=AUTH)
    assert r.status_code == 200
    assert "Чернетка відповіді</textarea>" in r.text
    assert f'<a href="{PDF["url"]}"' in r.text
    assert "Як експортувати звіт у PDF</a>" in r.text


async def test_list_filters_by_status_and_escapes(client):
    await add_ticket(1, message="<script>alert(1)</script>")
    await add_ticket(2, status="sent", final_message="Надіслано")
    r = await client.get("/ui?status=sent", auth=AUTH)
    assert "Надіслано" in r.text and "T-1" not in r.text
    r = await client.get("/ui", auth=AUTH)
    assert "&lt;script&gt;" in r.text and "<script>" not in r.text


async def test_list_paginates(client):
    for n in range(51):
        await add_ticket(n)
    r = await client.get("/ui", auth=AUTH)
    assert r.text.count('class="ticket"') == 50
    assert "offset=50" in r.text
    r = await client.get("/ui?offset=50", auth=AUTH)
    assert r.text.count('class="ticket"') == 1


async def test_approve_with_edited_text(client):
    ticket_id = await add_ticket(attempts=1)
    r = await post(
        client,
        f"/ui/tickets/{ticket_id}/approve",
        {"final_message": "  Відредагована відповідь  ", "filter": "draft"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui?notice=approved&status=draft"
    t = await get_ticket(ticket_id)
    assert t.status == "approved"
    assert t.final_message == "Відредагована відповідь"
    assert t.attempts == 0 and t.next_attempt_at is not None


async def test_double_approve_changes_state_once(client):
    ticket_id = await add_ticket()
    path = f"/ui/tickets/{ticket_id}/approve"
    first, second = await asyncio.gather(
        post(client, path, {"final_message": "Перша"}),
        post(client, path, {"final_message": "Друга"}),
    )
    notices = sorted(r.headers["location"] for r in (first, second))
    assert notices == ["/ui?notice=approved", "/ui?notice=stale"]
    t = await get_ticket(ticket_id)
    assert t.status == "approved" and t.final_message in ("Перша", "Друга")


@pytest.mark.parametrize("text", ["", "   ", "x" * 10_001])
async def test_approve_rejects_empty_or_too_long_text(client, text):
    ticket_id = await add_ticket()
    r = await post(client, f"/ui/tickets/{ticket_id}/approve", {"final_message": text})
    assert r.headers["location"] == "/ui?notice=invalid"
    assert (await get_ticket(ticket_id)).status == "draft"


async def test_reject(client):
    ticket_id = await add_ticket()
    r = await post(client, f"/ui/tickets/{ticket_id}/reject")
    assert r.headers["location"] == "/ui?notice=rejected"
    assert (await get_ticket(ticket_id)).status == "rejected"
    r = await post(client, f"/ui/tickets/{ticket_id}/approve", {"final_message": "x"})
    assert r.headers["location"] == "/ui?notice=stale"
    assert (await get_ticket(ticket_id)).status == "rejected"


@pytest.mark.parametrize(
    ("failed", "retried"), [("generation_failed", "new"), ("send_failed", "approved")]
)
async def test_retry_failed_ticket(client, failed, retried):
    ticket_id = await add_ticket(status=failed, attempts=5, last_error="boom")
    r = await post(client, f"/ui/tickets/{ticket_id}/retry")
    assert r.headers["location"] == "/ui?notice=retried"
    t = await get_ticket(ticket_id)
    assert t.status == retried and t.attempts == 0


async def test_retry_ignores_tickets_that_did_not_fail(client):
    ticket_id = await add_ticket()
    r = await post(client, f"/ui/tickets/{ticket_id}/retry")
    assert r.headers["location"] == "/ui?notice=stale"
    assert (await get_ticket(ticket_id)).status == "draft"


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Referer": "https://evil.example/page"},
        {"Origin": "null"},
        {},
    ],
)
async def test_cross_site_post_is_rejected(client, headers):
    ticket_id = await add_ticket()
    r = await post(client, f"/ui/tickets/{ticket_id}/approve", {"final_message": "x"}, headers)
    assert r.status_code == 403
    assert (await get_ticket(ticket_id)).status == "draft"


async def test_same_host_referer_is_accepted(client):
    ticket_id = await add_ticket()
    r = await post(client, f"/ui/tickets/{ticket_id}/reject", headers={"Referer": "http://test/ui"})
    assert r.status_code == 303
