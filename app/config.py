from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Every default works under `docker compose up`; override via env."""

    database_url: str = "postgresql+asyncpg://app:app@db:5432/app"
    public_base_url: str = "http://localhost:8000"


settings = Settings()
