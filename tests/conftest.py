"""Shared test fixtures for chat-recall-api."""

import pytest
from fastapi.testclient import TestClient

from chat_recall_api.main import app


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)
