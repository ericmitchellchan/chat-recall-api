"""Tests for multi-user data isolation (CR-39).

Core requirement: User A must NEVER see, modify, or delete User B's data.
Each test creates two users with different IDs and tokens, then verifies
that the SQL parameters contain the correct user_id for the authenticated user.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from chat_recall_api.config import Settings, get_settings
from chat_recall_api.deps import get_db
from chat_recall_api.main import app

from tests.test_users import TEST_SECRET, _make_jwe


# ── Constants ────────────────────────────────────────────────────────────

USER_A_ID = "user-aaa-1111"
USER_B_ID = "user-bbb-2222"

USER_A_ROW = {
    "id": USER_A_ID, "email": "alice@example.com", "name": "Alice",
    "github_id": "gh-alice", "google_id": None, "avatar_url": None,
    "created_at": "2024-01-01", "updated_at": None,
}
USER_B_ROW = {
    "id": USER_B_ID, "email": "bob@example.com", "name": "Bob",
    "github_id": "gh-bob", "google_id": None, "avatar_url": None,
    "created_at": "2024-02-01", "updated_at": None,
}


# ── Helpers ──────────────────────────────────────────────────────────────


def _token_for(user_id: str, email: str = "test@example.com") -> str:
    return _make_jwe({"sub": user_id, "email": email})


def _auth_header(user_id: str, email: str = "test@example.com") -> dict:
    return {"Authorization": f"Bearer {_token_for(user_id, email)}"}


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db_conn():
    conn = AsyncMock()
    conn.row_factory = None
    return conn


@pytest.fixture
def billing_settings():
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        nextauth_secret=TEST_SECRET,
        stripe_secret_key="sk_test_fake",
        stripe_webhook_secret="",
        stripe_monthly_price_id="price_monthly_test",
        stripe_annual_price_id="price_annual_test",
        stripe_product_id="prod_test",
        frontend_url="http://localhost:3002",
    )


@pytest.fixture
def client_with_mocks(mock_db_conn, billing_settings):
    async def override_get_db():
        yield mock_db_conn

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = lambda: billing_settings

    yield TestClient(app), mock_db_conn, billing_settings

    app.dependency_overrides.clear()


# ── GET /users/me — isolation ────────────────────────────────────────────


def test_get_me_returns_user_a_data(client_with_mocks):
    """User A's token returns User A's data, not User B's."""
    client, conn, settings = client_with_mocks

    captured_params = []

    async def mock_execute(sql, params=None):
        captured_params.append(params)
        m = AsyncMock()
        m.fetchone = AsyncMock(return_value=USER_A_ROW)
        return m

    conn.execute = mock_execute

    response = client.get("/users/me", headers=_auth_header(USER_A_ID, "alice@example.com"))

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == USER_A_ID
    assert data["email"] == "alice@example.com"
    # The SQL query must have used User A's ID
    assert any(USER_A_ID in (p or ()) for p in captured_params)


def test_get_me_returns_user_b_data(client_with_mocks):
    """User B's token returns User B's data, not User A's."""
    client, conn, settings = client_with_mocks

    captured_params = []

    async def mock_execute(sql, params=None):
        captured_params.append(params)
        m = AsyncMock()
        m.fetchone = AsyncMock(return_value=USER_B_ROW)
        return m

    conn.execute = mock_execute

    response = client.get("/users/me", headers=_auth_header(USER_B_ID, "bob@example.com"))

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == USER_B_ID
    assert data["email"] == "bob@example.com"
    assert any(USER_B_ID in (p or ()) for p in captured_params)


def test_get_me_two_users_see_own_data(client_with_mocks):
    """Two sequential requests with different tokens return their own data."""
    client, conn, settings = client_with_mocks

    call_user_ids = []

    async def mock_execute(sql, params=None):
        if params:
            call_user_ids.append(params[0])
        m = AsyncMock()
        # Return the right user based on the queried user_id
        if params and params[0] == USER_A_ID:
            m.fetchone = AsyncMock(return_value=USER_A_ROW)
        elif params and params[0] == USER_B_ID:
            m.fetchone = AsyncMock(return_value=USER_B_ROW)
        else:
            m.fetchone = AsyncMock(return_value=None)
        return m

    conn.execute = mock_execute

    resp_a = client.get("/users/me", headers=_auth_header(USER_A_ID))
    resp_b = client.get("/users/me", headers=_auth_header(USER_B_ID))

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert resp_a.json()["id"] == USER_A_ID
    assert resp_b.json()["id"] == USER_B_ID

    # Verify the DB was queried with the correct user_id each time
    assert call_user_ids[0] == USER_A_ID
    assert call_user_ids[1] == USER_B_ID


# ── PATCH /users/me — isolation ──────────────────────────────────────────


def test_patch_me_user_a_does_not_affect_user_b(client_with_mocks):
    """User A updating their profile only targets User A's row."""
    client, conn, settings = client_with_mocks

    captured_sql = []
    captured_params = []

    updated_a = {
        **USER_A_ROW,
        "name": "Alice Updated",
        "updated_at": "2024-06-01",
    }

    call_count = 0

    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        captured_sql.append(sql)
        captured_params.append(params)
        m = AsyncMock()
        if "UPDATE" in sql:
            return m
        # SELECT after UPDATE
        m.fetchone = AsyncMock(return_value=updated_a)
        return m

    conn.execute = mock_execute

    response = client.patch(
        "/users/me",
        json={"name": "Alice Updated"},
        headers=_auth_header(USER_A_ID),
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Alice Updated"

    # Verify UPDATE targeted User A's ID
    update_calls = [
        (sql, params) for sql, params in zip(captured_sql, captured_params)
        if "UPDATE" in sql
    ]
    assert len(update_calls) >= 1
    update_sql, update_params = update_calls[0]
    # user_id is the last param in the UPDATE ... WHERE id = %s
    assert USER_A_ID in update_params
    # User B's ID must NOT appear in any SQL params
    all_params = [p for params in captured_params if params for p in params]
    assert USER_B_ID not in all_params


def test_patch_me_user_b_updates_only_user_b(client_with_mocks):
    """User B updating their profile only targets User B's row."""
    client, conn, settings = client_with_mocks

    captured_params = []

    updated_b = {
        **USER_B_ROW,
        "name": "Bob Updated",
        "updated_at": "2024-06-01",
    }

    async def mock_execute(sql, params=None):
        captured_params.append(params)
        m = AsyncMock()
        if "UPDATE" in sql:
            return m
        m.fetchone = AsyncMock(return_value=updated_b)
        return m

    conn.execute = mock_execute

    response = client.patch(
        "/users/me",
        json={"name": "Bob Updated"},
        headers=_auth_header(USER_B_ID),
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Bob Updated"

    # Verify all DB calls use User B's ID, not User A's
    all_params = [p for params in captured_params if params for p in params]
    assert USER_B_ID in all_params
    assert USER_A_ID not in all_params


# ── DELETE /account — isolation ──────────────────────────────────────────


def test_delete_account_only_deletes_user_a(client_with_mocks):
    """Deleting User A's account only passes User A's ID to all DELETE queries."""
    client, conn, settings = client_with_mocks

    captured_sql = []
    captured_params = []

    call_count = 0

    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        captured_sql.append(sql)
        captured_params.append(params)
        m = AsyncMock()
        if call_count == 1:
            # SELECT id FROM users WHERE id = %s (verify user exists)
            m.fetchone = AsyncMock(return_value={"id": USER_A_ID})
        else:
            # DELETE statements
            m.rowcount = 0
        return m

    conn.execute = mock_execute

    response = client.delete("/account", headers=_auth_header(USER_A_ID))

    assert response.status_code == 200
    assert response.json()["deleted"] is True

    # Every SQL call must contain User A's ID, never User B's
    all_params = [p for params in captured_params if params for p in params]
    assert all(USER_A_ID in (params or ()) for params in captured_params)
    assert USER_B_ID not in all_params


def test_delete_account_user_a_does_not_touch_user_b(client_with_mocks):
    """Verify no DELETE query references User B's ID when User A deletes."""
    client, conn, settings = client_with_mocks

    delete_params = []

    call_count = 0

    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if call_count == 1:
            m.fetchone = AsyncMock(return_value={"id": USER_A_ID})
        else:
            m.rowcount = 2
            if "DELETE" in sql:
                delete_params.append(params)
        return m

    conn.execute = mock_execute

    response = client.delete("/account", headers=_auth_header(USER_A_ID))
    assert response.status_code == 200

    # All DELETE params must use User A's ID only
    for params in delete_params:
        flat = list(params) if params else []
        assert USER_A_ID in flat, f"DELETE query missing User A's ID: {params}"
        assert USER_B_ID not in flat, f"DELETE query contains User B's ID: {params}"


# ── GET /billing/status — isolation ──────────────────────────────────────


def test_billing_status_user_a_sees_own(client_with_mocks):
    """User A sees only their own billing status."""
    client, conn, settings = client_with_mocks

    captured_params = []

    call_count = 0

    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        captured_params.append(params)
        m = AsyncMock()
        if call_count == 1:
            # users query
            m.fetchone = AsyncMock(return_value={
                "trial_ends_at": "2024-01-15",
                "subscription_status": "active",
            })
        else:
            # subscriptions query
            m.fetchone = AsyncMock(return_value={
                "plan": "pro",
                "status": "active",
                "current_period_end": "2024-02-15",
                "stripe_subscription_id": "sub_alice",
            })
        return m

    conn.execute = mock_execute

    response = client.get("/billing/status", headers=_auth_header(USER_A_ID))

    assert response.status_code == 200
    data = response.json()
    assert data["subscription_status"] == "active"
    assert data["stripe_subscription_id"] == "sub_alice"

    # All SQL queries must use User A's ID
    for params in captured_params:
        assert params is not None
        assert USER_A_ID in params
        assert USER_B_ID not in params


def test_billing_status_user_b_sees_own(client_with_mocks):
    """User B sees only their own billing status (different from User A)."""
    client, conn, settings = client_with_mocks

    captured_params = []

    call_count = 0

    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        captured_params.append(params)
        m = AsyncMock()
        if call_count == 1:
            m.fetchone = AsyncMock(return_value={
                "trial_ends_at": "2024-03-01",
                "subscription_status": "trial",
            })
        else:
            m.fetchone = AsyncMock(return_value=None)
        return m

    conn.execute = mock_execute

    response = client.get("/billing/status", headers=_auth_header(USER_B_ID))

    assert response.status_code == 200
    data = response.json()
    assert data["subscription_status"] == "trial"
    assert data["plan"] is None

    for params in captured_params:
        assert params is not None
        assert USER_B_ID in params
        assert USER_A_ID not in params


def test_billing_status_two_users_isolated(client_with_mocks):
    """Sequential billing/status calls use the authenticated user's ID each time."""
    client, conn, settings = client_with_mocks

    query_user_ids = []

    async def mock_execute(sql, params=None):
        if params:
            query_user_ids.append(params[0])
        m = AsyncMock()
        m.fetchone = AsyncMock(return_value={
            "trial_ends_at": None,
            "subscription_status": "active",
            "plan": "pro",
            "status": "active",
            "current_period_end": "2025-01-01",
            "stripe_subscription_id": "sub_test",
        })
        return m

    conn.execute = mock_execute

    client.get("/billing/status", headers=_auth_header(USER_A_ID))
    # Reset query tracking
    user_a_queries = list(query_user_ids)
    query_user_ids.clear()

    conn.execute = mock_execute
    client.get("/billing/status", headers=_auth_header(USER_B_ID))
    user_b_queries = list(query_user_ids)

    # User A's request must only query with User A's ID
    assert all(uid == USER_A_ID for uid in user_a_queries)
    # User B's request must only query with User B's ID
    assert all(uid == USER_B_ID for uid in user_b_queries)


# ── POST /billing/cancel — isolation ─────────────────────────────────────


def test_billing_cancel_user_a_only_cancels_own(client_with_mocks):
    """User A cancelling only queries with User A's ID."""
    client, conn, settings = client_with_mocks

    from unittest.mock import patch, MagicMock

    captured_params = []

    async def mock_execute(sql, params=None):
        captured_params.append(params)
        m = AsyncMock()
        m.fetchone = AsyncMock(return_value={
            "stripe_subscription_id": "sub_alice_cancel"
        })
        return m

    conn.execute = mock_execute

    with patch("chat_recall_api.routers.billing.stripe") as mock_stripe:
        response = client.post("/billing/cancel", headers=_auth_header(USER_A_ID))

    assert response.status_code == 200
    assert response.json()["cancelled"] is True

    # Verify the subscription lookup used User A's ID
    for params in captured_params:
        assert params is not None
        assert USER_A_ID in params
        assert USER_B_ID not in params

    # Verify Stripe was called with User A's subscription
    mock_stripe.Subscription.modify.assert_called_once_with(
        "sub_alice_cancel", cancel_at_period_end=True,
    )


def test_billing_cancel_user_b_only_cancels_own(client_with_mocks):
    """User B cancelling only queries with User B's ID."""
    client, conn, settings = client_with_mocks

    from unittest.mock import patch

    captured_params = []

    async def mock_execute(sql, params=None):
        captured_params.append(params)
        m = AsyncMock()
        m.fetchone = AsyncMock(return_value={
            "stripe_subscription_id": "sub_bob_cancel"
        })
        return m

    conn.execute = mock_execute

    with patch("chat_recall_api.routers.billing.stripe") as mock_stripe:
        response = client.post("/billing/cancel", headers=_auth_header(USER_B_ID))

    assert response.status_code == 200

    for params in captured_params:
        assert params is not None
        assert USER_B_ID in params
        assert USER_A_ID not in params

    mock_stripe.Subscription.modify.assert_called_once_with(
        "sub_bob_cancel", cancel_at_period_end=True,
    )
