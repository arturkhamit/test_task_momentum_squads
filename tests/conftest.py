import asyncio
import os
from pathlib import Path

# Point the app at the test database before anything imports app.config.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://app:app@127.0.0.1:55432/app_test"
)

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import make_url, text  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.seed_kb import seed  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
async def database():
    """Recreate the test database, apply migrations and seed the KB once per run."""
    url = make_url(os.environ["DATABASE_URL"])
    assert url.database.endswith("_test"), "refusing to recreate a non-test database"
    admin_dsn = url.set(drivername="postgresql", database="postgres")
    conn = await asyncpg.connect(admin_dsn.render_as_string(hide_password=False))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()
    # Alembic's env.py calls asyncio.run(), so it needs its own thread.
    await asyncio.to_thread(command.upgrade, Config(str(ROOT / "alembic.ini")), "head")
    async with SessionLocal() as session:
        await seed(session)
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_tickets():
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE tickets"))


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
