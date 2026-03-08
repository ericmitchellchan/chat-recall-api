"""Tests for the stats endpoint (GET /stats)."""

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from chat_recall_api.config import Settings, get_settings
from chat_recall_api.deps import get_db
from chat_recall_api.main import app

from tests.test_users import TEST_SECRET, _make_jwe


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db_conn():
    conn = AsyncMock()
    conn.row_factory = None
    return conn


@pytest.fixture
def client_with_mocks(mock_db_conn):
    settings = Settings(
        database_url="postgresql://test:test@localhost/test",
        nextauth_secret=TEST_SECRET,
    )

    async def override_get_db():
        yield mock_db_conn

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = lambda: settings

    yield TestClient(app), mock_db_conn, settings

    app.dependency_overrides.clear()


def _auth_header(user_id="user-uuid"):
    token = _make_jwe({"sub": user_id, "email": "test@example.com"})
    return {"Authorization": f"Bearer {token}"}


# ── GET /stats — active subscription ─────────────────────────────────────


def test_stats_active_subscription(client_with_mocks):
    client, conn, settings = client_with_mocks

    call_count = 0

    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if call_count == 1:
            # users query
            m.fetchone = AsyncMock(return_value={
                "total_conversations": 1234,
                "total_messages": 45678,
                "total_uploads": 3,
                "last_upload_at": "2026-03-05T10:00:00",
                "subscription_status": "active",
                "trial_ends_at": None,
            })
        elif call_count == 2:
            # conversations COUNT query
            m.fetchone = AsyncMock(return_value={"cnt": 1234})
        else:
            # uploads query
            m.fetchall = AsyncMock(return_value=[
                {
                    "id": "upload-1",
                    "filename": "conversations.json",
                    "status": "completed",
                    "conversations_imported": 500,
                    "messages_imported": 12000,
                    "created_at": "2026-03-05T10:00:00",
                },
            ])
        return m

    conn.execute = mock_execute

    response = client.get("/stats", headers=_auth_header())

    assert response.status_code == 200
    data = response.json()
    assert data["total_conversations"] == 1234
    assert data["total_messages"] == 45678
    assert data["total_uploads"] == 3
    assert data["last_upload_at"] == "2026-03-05T10:00:00"
    assert data["subscription_status"] == "active"
    assert data["trial_ends_at"] is None
    assert data["trial_days_remaining"] is None
    assert data["storage_conversations"] == 1234
    assert len(data["recent_uploads"]) == 1
    assert data["recent_uploads"][0]["id"] == "upload-1"
    assert data["recent_uploads"][0]["filename"] == "conversations.json"


# ── GET /stats — trial user with days remaining ──────────────────────────


def test_stats_trial_user(client_with_mocks):
    client, conn, settings = client_with_mocks

    # Trial ending 13 days from today
    trial_end = (date.today() + timedelta(days=13)).isoformat()

    call_count = 0

    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if call_count == 1:
            m.fetchone = AsyncMock(return_value={
                "total_conversations": 50,
                "total_messages": 1200,
                "total_uploads": 1,
                "last_upload_at": "2026-03-01T08:00:00",
                "subscription_status": "trial",
                "trial_ends_at": trial_end,
            })
        elif call_count == 2:
            m.fetchone = AsyncMock(return_value={"cnt": 50})
        else:
            m.fetchall = AsyncMock(return_value=[])
        return m

    conn.execute = mock_execute

    response = client.get("/stats", headers=_auth_header())

    assert response.status_code == 200
    data = response.json()
    assert data["subscription_status"] == "trial"
    assert data["trial_ends_at"] == trial_end
    assert data["trial_days_remaining"] == 13
    assert data["total_conversations"] == 50
    assert data["recent_uploads"] == []


# ── GET /stats — no subscription (expired trial) ────────────────────────


def test_stats_no_subscription(client_with_mocks):
    client, conn, settings = client_with_mocks

    # Trial already expired
    trial_end = (date.today() - timedelta(days=5)).isoformat()

    call_count = 0

    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if call_count == 1:
            m.fetchone = AsyncMock(return_value={
                "total_conversations": 0,
                "total_messages": 0,
                "total_uploads": 0,
                "last_upload_at": None,
                "subscription_status": "expired",
                "trial_ends_at": trial_end,
            })
        elif call_count == 2:
            m.fetchone = AsyncMock(return_value={"cnt": 0})
        else:
            m.fetchall = AsyncMock(return_value=[])
        return m

    conn.execute = mock_execute

    response = client.get("/stats", headers=_auth_header())

    assert response.status_code == 200
    data = response.json()
    assert data["subscription_status"] == "expired"
    assert data["trial_days_remaining"] == 0
    assert data["last_upload_at"] is None
    assert data["total_conversations"] == 0


# ── GET /stats — includes recent uploads ─────────────────────────────────


def test_stats_includes_recent_uploads(client_with_mocks):
    client, conn, settings = client_with_mocks

    uploads = [
        {
            "id": f"upload-{i}",
            "filename": f"file-{i}.json",
            "status": "completed",
            "conversations_imported": 100 * i,
            "messages_imported": 2000 * i,
            "created_at": f"2026-03-0{i}T10:00:00",
        }
        for i in range(1, 6)
    ]

    call_count = 0

    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if call_count == 1:
            m.fetchone = AsyncMock(return_value={
                "total_conversations": 500,
                "total_messages": 10000,
                "total_uploads": 5,
                "last_upload_at": "2026-03-05T10:00:00",
                "subscription_status": "active",
                "trial_ends_at": None,
            })
        elif call_count == 2:
            m.fetchone = AsyncMock(return_value={"cnt": 500})
        else:
            m.fetchall = AsyncMock(return_value=uploads)
        return m

    conn.execute = mock_execute

    response = client.get("/stats", headers=_auth_header())

    assert response.status_code == 200
    data = response.json()
    assert len(data["recent_uploads"]) == 5
    for i, upload in enumerate(data["recent_uploads"], start=1):
        assert upload["id"] == f"upload-{i}"
        assert upload["filename"] == f"file-{i}.json"
        assert upload["status"] == "completed"
        assert upload["conversations_imported"] == 100 * i
        assert upload["messages_imported"] == 2000 * i


# ── GET /stats — user not found (404) ────────────────────────────────────


def test_stats_user_not_found(client_with_mocks):
    client, conn, settings = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/stats", headers=_auth_header("nonexistent-uuid"))

    assert response.status_code == 404


# ── GET /stats — unauthorized (401 without token) ────────────────────────


def test_stats_unauthorized(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.get("/stats")
    assert response.status_code == 401
