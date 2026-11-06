from pydantic import PositiveFloat, PositiveInt
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Every default works under `docker compose up`; override via env."""

    database_url: str = "postgresql+asyncpg://app:app@db:5432/app"
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    # Operator UI (HTTP Basic Auth). Change both in production.
    operator_user: str = "admin"
    operator_password: str = "admin"

    # LLM. An empty key means no-key mode: real request, expected 401, static draft.
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"
    llm_rpm: PositiveInt = 60  # request starts per minute for the whole process
    llm_timeout: PositiveFloat = 60.0
    stub_delay_min: float = 5.0
    stub_delay_max: float = 30.0

    # Generation worker.
    llm_concurrency: PositiveInt = 20
    gen_max_attempts: PositiveInt = 5
    # Must exceed the worst case of one job: limiter wait (up to llm_concurrency * 60 / llm_rpm
    # = 20 s) + llm_timeout (60 s) + stub delay (30 s), with a wide margin for DB hiccups.
    lease_seconds: PositiveInt = 300
    poll_interval: PositiveFloat = 1.0
    shutdown_grace: float = 8.0  # below Docker's default 10 s stop timeout

    # Sending approved replies back to the helpdesk.
    send_concurrency: PositiveInt = 5
    send_timeout: PositiveFloat = 10.0
    send_lease_seconds: PositiveInt = 60  # must exceed send_timeout
    send_max_attempts: PositiveInt = 10
    # reply_url comes from outside (SSRF): its host must be one of these (comma-separated).
    helpdesk_allowed_hosts: str = "helpdesk.example.com"
    # When set, reply_url's scheme and host are replaced with this (the path is kept).
    helpdesk_mock_url: str = ""


settings = Settings()
