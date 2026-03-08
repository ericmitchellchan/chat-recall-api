"""Tests for subscription/trial access control middleware."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from chat_recall_api.access import AccessInfo, require_active_access
from chat_recall_api.config import Settings, get_settings
from chat_recall_api.deps import get_db
from chat_recall_api.main import app

from tests.test_users import TEST_SECRET, _make_jwe


# ── Create a test-only protected route ────────────────────────────────────

_test_router = APIRouter()


@_test_router.get("/test-protected")
async def protected_route(access: AccessInfo = Depends(require_active_access)):
    return {
        "allowed": access.allowed,
        "reason": access.reason,
        "user_id": access.user_id,
    }


# Register once (idempotent due to prefix)
app.include_router(_test_router)


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

    yield TestClient(app), mock_db_conn

    app.dependency_overrides.clear()


def _auth_header(user_id="user-uuid"):
    token = _make_jwe({"sub": user_id, "email": "test@example.com"})
    return {"Authorization": f"Bearer {token}"}


def _future(days=7):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _past(days=7):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ── Active subscription ──────────────────────────────────────────────────


def test_active_subscription_allowed(client_with_mocks):
    client, conn = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value={
        "subscription_status": "active",
        "trial_ends_at": None,
    })
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 200
    data = response.json()
    assert data["allowed"] is True
    assert data["reason"] == "subscription"


# ── Active trial ─────────────────────────────────────────────────────────


def test_active_trial_allowed(client_with_mocks):
    client, conn = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value={
        "subscription_status": "trial",
        "trial_ends_at": _future(days=10),
    })
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 200
    data = response.json()
    assert data["allowed"] is True
    assert data["reason"] == "trial"


def test_expired_trial_blocked(client_with_mocks):
    client, conn = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value={
        "subscription_status": "trial",
        "trial_ends_at": _past(days=1),
    })
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 402
    assert "trial has expired" in response.json()["detail"]


# ── Past due ─────────────────────────────────────────────────────────────


def test_past_due_allowed(client_with_mocks):
    """Past due users keep access while Stripe retries payment."""
    client, conn = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value={
        "subscription_status": "past_due",
        "trial_ends_at": None,
    })
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 200
    assert response.json()["reason"] == "past_due"


# ── Cancelled with active period ─────────────────────────────────────────


def test_cancelled_within_period_allowed(client_with_mocks):
    """Cancelled but billing period hasn't ended yet."""
    client, conn = client_with_mocks

    call_count = 0
    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if call_count == 1:
            # users query
            m.fetchone = AsyncMock(return_value={
                "subscription_status": "cancelled",
                "trial_ends_at": None,
            })
        else:
            # subscriptions query
            m.fetchone = AsyncMock(return_value={
                "current_period_end": _future(days=15),
            })
        return m

    conn.execute = mock_execute

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 200
    assert response.json()["reason"] == "cancelled_grace"


def test_cancelled_past_period_blocked(client_with_mocks):
    """Cancelled and billing period has ended."""
    client, conn = client_with_mocks

    call_count = 0
    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if call_count == 1:
            m.fetchone = AsyncMock(return_value={
                "subscription_status": "cancelled",
                "trial_ends_at": None,
            })
        else:
            m.fetchone = AsyncMock(return_value={
                "current_period_end": _past(days=5),
            })
        return m

    conn.execute = mock_execute

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 402
    assert "ended" in response.json()["detail"]


# ── No subscription ─────────────────────────────────────────────────────


def test_no_subscription_blocked(client_with_mocks):
    client, conn = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value={
        "subscription_status": "none",
        "trial_ends_at": None,
    })
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 402
    assert "subscription is required" in response.json()["detail"]


def test_null_status_blocked(client_with_mocks):
    client, conn = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value={
        "subscription_status": None,
        "trial_ends_at": None,
    })
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 402


# ── User not found ───────────────────────────────────────────────────────


def test_user_not_found_404(client_with_mocks):
    client, conn = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 404


# ── Unauthorized ─────────────────────────────────────────────────────────


def test_unauthorized_no_token(client_with_mocks):
    client, conn = client_with_mocks

    response = client.get("/test-protected")

    assert response.status_code == 401


# ── Datetime edge cases ─────────────────────────────────────────────────


def test_trial_with_datetime_object(client_with_mocks):
    """trial_ends_at as actual datetime object (not string)."""
    client, conn = client_with_mocks

    future_dt = datetime.now(timezone.utc) + timedelta(days=5)
    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value={
        "subscription_status": "trial",
        "trial_ends_at": future_dt,
    })
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 200
    assert response.json()["reason"] == "trial"


def test_trial_with_naive_datetime(client_with_mocks):
    """trial_ends_at as naive datetime (no timezone) still works."""
    client, conn = client_with_mocks

    # Naive datetime far in the future
    future_dt = datetime(2099, 1, 1, 0, 0, 0)
    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value={
        "subscription_status": "trial",
        "trial_ends_at": future_dt,
    })
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/test-protected", headers=_auth_header())

    assert response.status_code == 200
    assert response.json()["reason"] == "trial"
