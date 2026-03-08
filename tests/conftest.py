"""Shared test fixtures for chat-recall-api."""

import pytest
from fastapi.testclient import TestClient

from chat_recall_api.main import app
from chat_recall_api.ratelimit import get_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset rate limiter before each test to prevent cross-test interference."""
    get_limiter().reset()
    yield
    get_limiter().reset()


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)
