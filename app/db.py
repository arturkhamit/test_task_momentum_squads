from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# Fail fast when Postgres is unreachable instead of asyncpg's 60 s default.
engine = create_async_engine(settings.database_url, pool_pre_ping=True, connect_args={"timeout": 5})
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
