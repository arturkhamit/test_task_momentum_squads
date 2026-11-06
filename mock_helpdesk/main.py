"""Fake helpdesk API with flaky behaviour: uvicorn mock_helpdesk.main:app --port 8001

Single process, in-memory store. Deduplicates replies by Idempotency-Key like a real
helpdesk must for exactly-once delivery.
"""

import asyncio
import html
import random
from collections import Counter
from datetime import UTC, datetime

from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MockSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_")

    fail_rate: float = Field(0.2, ge=0, le=1)  # 503 without storing
    slow_rate: float = Field(0.1, ge=0, le=1)  # store, then answer after slow_seconds
    slow_seconds: float = 15.0


class Reply(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=20_000)


settings = MockSettings()
app = FastAPI(title="Mock helpdesk")
records: list[dict] = []
by_key: dict[str, dict] = {}
attempts: Counter[str] = Counter()
lock = asyncio.Lock()


def view(record: dict) -> dict:
    key = record["idempotency_key"]
    return record | {"attempts": attempts[key] if key else 1}


@app.post("/tickets/{ticket_id}/replies", status_code=201)
async def receive_reply(
    ticket_id: str, reply: Reply, idempotency_key: str | None = Header(None)
) -> dict:
    async with lock:
        if idempotency_key:
            attempts[idempotency_key] += 1
            if idempotency_key in by_key:
                return view(by_key[idempotency_key])
        if random.random() < settings.fail_rate:
            return JSONResponse({"error": "temporarily unavailable"}, status_code=503)
        record = {
            "id": len(records) + 1,
            "path_ticket_id": ticket_id,
            "ticket_id": reply.ticket_id,
            "message": reply.message,
            "idempotency_key": idempotency_key,
            "received_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        records.append(record)
        if idempotency_key:
            by_key[idempotency_key] = record
        slow = random.random() < settings.slow_rate
    if slow:
        # Stored, but the caller times out first: "processed, response lost".
        await asyncio.sleep(settings.slow_seconds)
    return view(record)


@app.get("/replies")
async def list_replies() -> dict:
    return {"replies": [view(r) for r in records]}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    per_ticket = Counter(r["path_ticket_id"] for r in records)
    rows = []
    for r in reversed(records):
        duplicate = per_ticket[r["path_ticket_id"]] > 1
        retries = view(r)["attempts"] - 1
        rows.append(
            f'<tr class="{"dup" if duplicate else ""}">'
            f"<td>{html.escape(r['path_ticket_id'])}{' DUPLICATE' if duplicate else ''}</td>"
            f"<td><pre>{html.escape(r['message'])}</pre></td>"
            f"<td>{r['received_at']}</td><td>{retries}</td></tr>"
        )
    duplicates = sum(1 for n in per_ticket.values() if n > 1)
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Mock helpdesk</title>'
        "<style>body{font:14px system-ui;margin:1.5rem}td,th{border:1px solid #ccc;"
        "padding:.3rem .5rem;vertical-align:top}pre{white-space:pre-wrap;margin:0}"
        ".dup{background:#fecaca;font-weight:bold}</style></head><body>"
        f"<h1>Mock helpdesk</h1><p>{len(records)} replies, {duplicates} tickets with "
        f"duplicates. fail_rate={settings.fail_rate}, slow_rate={settings.slow_rate}</p>"
        "<table><tr><th>Ticket</th><th>Message</th><th>Received</th>"
        f"<th>Retries absorbed</th></tr>{''.join(rows)}</table></body></html>"
    )
