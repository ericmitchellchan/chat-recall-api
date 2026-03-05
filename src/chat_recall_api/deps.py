"""FastAPI dependency injection."""

from chat_recall_api.config import Settings, get_settings


async def get_db():
    """Get database connection. Stub — will connect to chat-recall-prod's db layer."""
    # TODO: Initialize from chat-recall-prod db pool
    raise NotImplementedError("Database not configured yet")


def get_current_settings() -> Settings:
    """Get application settings."""
    return get_settings()
