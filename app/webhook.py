import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import Ticket

log = logging.getLogger(__name__)
router = APIRouter()


class HelpdeskEvent(BaseModel):
    # Helpdesks often send numeric ids; accept them as strings.
    model_config = ConfigDict(coerce_numbers_to_str=True, str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=200)
    ticket_id: str = Field(min_length=1, max_length=200)
    customer_email: str = Field(max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    message: str = Field(min_length=1, max_length=10_000)
    # The host allowlist is enforced at send time, not here.
    reply_url: HttpUrl = Field(max_length=2048)


@router.post("/webhooks/helpdesk")
async def helpdesk_webhook(
    event: HelpdeskEvent, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, str]:
    # The only work in the request: one idempotent insert. The worker does the rest.
    try:
        # Bounded so a hung DB cannot push the answer past the helpdesk's 2 s retry window.
        async with asyncio.timeout(settings.webhook_db_timeout):
            await session.execute(
                insert(Ticket)
                .values(
                    event_id=event.event_id,
                    ticket_id=event.ticket_id,
                    customer_email=event.customer_email,
                    message=event.message,
                    reply_url=str(event.reply_url),
                    status="new",
                    next_attempt_at=func.now(),
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
            )
            await session.commit()
    except (DBAPIError, OSError, TimeoutError) as e:
        # Not acknowledged, so the helpdesk retries; a duplicate later is a no-op.
        log.warning(
            "webhook could not store the ticket",
            extra={"event": "webhook_db_error", "error": type(e).__name__},
        )
        raise HTTPException(
            503, "Database unavailable, retry later", {"Retry-After": "5"}
        ) from None
    return {"status": "accepted"}
