import asyncio
import os
from pathlib import Path

# Point the app at the test database before anything imports app.config.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://app:app@127.0.0.1:55432/app_test"
)
# Tests never talk to the real provider: no-key mode unless a test opts into a fake key.
os.environ["ANTHROPIC_API_KEY"] = ""

import asyncpg  # noqa: E402
import httpx2  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import make_url, text  # noqa: E402

from app import llm  # noqa: E402
from app.config import settings  # noqa: E402
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


class CountingLimiter(llm.Limiter):
    calls = 0

    async def acquire(self) -> None:
        self.calls += 1
        await super().acquire()


@pytest.fixture(autouse=True)
def limiter(monkeypatch):
    """A fast limiter per test that counts acquires."""
    fast = CountingLimiter(rpm=600_000)
    monkeypatch.setattr(llm, "limiter", fast)
    return fast


@pytest.fixture
def anthropic_stub(monkeypatch):
    """Route the real SDK client to an in-process httpx2 handler; nothing leaves the machine.

    install(handler, api_key=None) returns the list of captured requests. A non-empty api_key
    switches the app out of no-key mode.
    """

    def install(handler, api_key=None):
        captured = []

        def record(request):
            captured.append(request)
            return handler(request)

        options = {"http_client": httpx2.AsyncClient(transport=httpx2.MockTransport(record))}
        if api_key:
            options["api_key"] = api_key
            monkeypatch.setattr(settings, "anthropic_api_key", api_key)
        monkeypatch.setattr(llm, "client", llm.client.with_options(**options))
        return captured

    return install


@pytest.fixture
def fake_sleep(monkeypatch):
    """Patch asyncio.sleep so the 5-30 s no-key delay is recorded instead of waited."""
    delays = []
    real_sleep = asyncio.sleep

    async def sleep(delay, *args, **kwargs):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return delays


def message_response(text="Відповідь", stop_reason="end_turn"):
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [
                {"type": "thinking", "thinking": "internal", "signature": "sig"},
                {"type": "text", "text": text},
            ],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


def error_response(status, error_type="api_error", headers=None):
    body = {"type": "error", "error": {"type": error_type, "message": f"stub {status}"}}
    return httpx2.Response(status, json=body, headers=headers)
