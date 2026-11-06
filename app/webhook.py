import asyncio
import logging
import math
import time
from collections import OrderedDict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
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


class RateLimiter:
    """Token bucket per client IP: `burst` requests at once, refilled at `per_minute`.

    In-process, so the limit is per api process (we run one). Memory is bounded: past
    `max_clients` tracked IPs the least recently seen one is forgotten.
    """

    def __init__(self, per_minute: int, burst: int, max_clients: int = 10_000, clock=None):
        self.enabled = per_minute > 0 and burst > 0
        self.rate = per_minute / 60
        self.burst = burst
        self.max_clients = max_clients
        self.clock = clock or time.monotonic
        self.buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()  # ip -> tokens, at

    def take(self, key: str) -> float:
        """Take a token: 0 when allowed, else the seconds until the next token."""
        if not self.enabled:
            return 0.0
        now = self.clock()
        tokens, at = self.buckets.pop(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - at) * self.rate)
        if tokens >= 1:
            tokens, wait = tokens - 1, 0.0
        else:
            wait = (1 - tokens) / self.rate
        self.buckets[key] = (tokens, now)  # re-inserted: now the most recently seen
        if len(self.buckets) > self.max_clients:
            self.buckets.popitem(last=False)
        return wait


limiter = RateLimiter(settings.webhook_rate_per_min, settings.webhook_burst)


def rate_limit(request: Request) -> None:
    # request.client is the real client only because uvicorn runs with --proxy-headers and the
    # api port is reachable solely through the proxy; exposed directly, X-Forwarded-For could
    # be spoofed to dodge the limit.
    wait = limiter.take(request.client.host if request.client else "unknown")
    if wait:
        raise HTTPException(
            429, "Too many requests", headers={"Retry-After": str(max(1, math.ceil(wait)))}
        )


class HelpdeskEvent(BaseModel):
    # Helpdesks often send numeric ids; accept them as strings.
    model_config = ConfigDict(coerce_numbers_to_str=True, str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=200)
    ticket_id: str = Field(min_length=1, max_length=200)
    customer_email: str = Field(max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    message: str = Field(min_length=1, max_length=10_000)
    # The host allowlist is enforced at send time, not here.
    reply_url: HttpUrl = Field(max_length=2048)


@router.post("/webhooks/helpdesk", dependencies=[Depends(rate_limit)])
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
