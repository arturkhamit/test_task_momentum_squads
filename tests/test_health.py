from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import app


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.text == "OK"


async def test_ready(client):
    r = await client.get("/ready")
    assert r.status_code == 200


async def test_ready_503_when_db_unreachable(client):
    # Nothing listens on port 1, so the connection is refused immediately.
    bad = create_async_engine("postgresql+asyncpg://app:app@127.0.0.1:1/app")

    async def broken_session():
        async with async_sessionmaker(bad)() as session:
            yield session

    app.dependency_overrides[get_session] = broken_session
    try:
        r = await client.get("/ready")
    finally:
        app.dependency_overrides.clear()
        await bad.dispose()
    assert r.status_code == 503
