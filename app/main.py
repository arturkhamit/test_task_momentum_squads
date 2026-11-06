import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import engine, get_session


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


app = FastAPI(title="Support drafts", lifespan=lifespan)


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "OK"


@app.get("/ready", response_class=PlainTextResponse)
async def ready(session: Annotated[AsyncSession, Depends(get_session)]) -> Response:
    try:
        async with asyncio.timeout(3):
            await session.execute(text("SELECT 1"))
    except Exception:
        return PlainTextResponse("DB unavailable", status_code=503)
    return PlainTextResponse("OK")
