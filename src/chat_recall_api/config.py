"""Configuration via environment variables using pydantic-settings."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    database_url: str = "postgresql://user:pass@localhost:5432/chat_recall"
    nextauth_secret: str = ""
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    cors_origins: str = "http://localhost:3000"
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
