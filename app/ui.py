import secrets
import uuid
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import STATUSES, Ticket

PAGE_SIZE = 50
MAX_MESSAGE = 10_000
NOTICES = {
    "approved": "Approved: the reply is queued for sending.",
    "rejected": "Rejected.",
    "retried": "Queued for another try.",
    "stale": "Nothing changed: the ticket was already handled (see its current status).",
    "invalid": f"The reply must be 1 to {MAX_MESSAGE} characters.",
}

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
basic = HTTPBasic(realm="Support drafts")


def require_operator(credentials: Annotated[HTTPBasicCredentials, Depends(basic)]) -> None:
    user_ok = secrets.compare_digest(credentials.username.encode(), settings.operator_user.encode())
    password_ok = secrets.compare_digest(
        credentials.password.encode(), settings.operator_password.encode()
    )
    if not (user_ok and password_ok):
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})


def same_origin(request: Request) -> None:
    """CSRF guard: the browser attaches Basic Auth to cross-site POSTs too, so only accept
    forms whose Origin (or Referer) is this host."""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source or urlsplit(source).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-site request rejected")


router = APIRouter(dependencies=[Depends(require_operator)])
Session = Annotated[AsyncSession, Depends(get_session)]
StatusFilter = Annotated[str, Form(alias="filter")]


def back_to_list(status_filter: str, notice: str) -> RedirectResponse:
    query = {"notice": notice}
    if status_filter in STATUSES:
        query["status"] = status_filter
    return RedirectResponse(f"/ui?{urlencode(query)}", status_code=303)


async def transition(session: AsyncSession, ticket_id: uuid.UUID, from_statuses, **values):
    """Conditional status change: a double click or a second operator changes nothing."""
    result = await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status.in_(from_statuses))
        .values(updated_at=func.now(), **values)
    )
    await session.commit()
    return result.rowcount == 1


@router.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/ui", status_code=303)


@router.get("/ui", response_class=HTMLResponse)
async def ticket_list(
    request: Request,
    session: Session,
    status: Annotated[str, Query()] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    notice: Annotated[str, Query()] = "",
):
    query = select(Ticket).order_by(Ticket.created_at.desc(), Ticket.id.desc())
    if status in STATUSES:
        query = query.where(Ticket.status == status)
    else:
        status = ""
    rows = (await session.scalars(query.offset(offset).limit(PAGE_SIZE + 1))).all()
    older = {"status": status, "offset": offset + PAGE_SIZE} if len(rows) > PAGE_SIZE else None
    return templates.TemplateResponse(
        request,
        "ui.html",
        {
            "tickets": rows[:PAGE_SIZE],
            "statuses": STATUSES,
            "status": status,
            "notice": NOTICES.get(notice),
            "older_query": urlencode(older) if older else None,
        },
    )


@router.post("/ui/tickets/{ticket_id}/approve", dependencies=[Depends(same_origin)])
async def approve(
    ticket_id: uuid.UUID,
    session: Session,
    final_message: Annotated[str, Form()] = "",
    status_filter: StatusFilter = "",
):
    message = final_message.strip()
    if not 0 < len(message) <= MAX_MESSAGE:
        return back_to_list(status_filter, "invalid")
    changed = await transition(
        session,
        ticket_id,
        ["draft"],
        status="approved",
        final_message=message,
        attempts=0,
        next_attempt_at=func.now(),
        last_error=None,
    )
    return back_to_list(status_filter, "approved" if changed else "stale")


@router.post("/ui/tickets/{ticket_id}/reject", dependencies=[Depends(same_origin)])
async def reject(ticket_id: uuid.UUID, session: Session, status_filter: StatusFilter = ""):
    changed = await transition(session, ticket_id, ["draft"], status="rejected")
    return back_to_list(status_filter, "rejected" if changed else "stale")


@router.post("/ui/tickets/{ticket_id}/retry", dependencies=[Depends(same_origin)])
async def retry(ticket_id: uuid.UUID, session: Session, status_filter: StatusFilter = ""):
    changed = await transition(
        session,
        ticket_id,
        ["generation_failed", "send_failed"],
        status=case((Ticket.status == "generation_failed", "new"), else_="approved"),
        attempts=0,
        next_attempt_at=func.now(),
    )
    return back_to_list(status_filter, "retried" if changed else "stale")
