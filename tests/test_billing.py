"""Tests for billing endpoints (checkout, status, cancel, webhooks)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

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


def _auth_header(user_id="user-uuid"):
    token = _make_jwe({"sub": user_id, "email": "test@example.com"})
    return {"Authorization": f"Bearer {token}"}


# ── POST /billing/checkout ───────────────────────────────────────────────


@patch("chat_recall_api.routers.billing.stripe")
def test_checkout_monthly(mock_stripe, client_with_mocks):
    client, conn, settings = client_with_mocks

    # No existing subscription
    mock_cur_empty = AsyncMock()
    mock_cur_empty.fetchone = AsyncMock(return_value=None)

    # User exists
    mock_cur_user = AsyncMock()
    mock_cur_user.fetchone = AsyncMock(return_value={"email": "test@example.com"})

    call_count = 0
    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        if "subscriptions" in sql:
            return mock_cur_empty
        return mock_cur_user

    conn.execute = mock_execute

    mock_stripe.Customer.create.return_value = MagicMock(id="cus_new")
    mock_stripe.checkout.Session.create.return_value = MagicMock(
        url="https://checkout.stripe.com/test"
    )

    response = client.post(
        "/billing/checkout",
        json={"plan": "monthly"},
        headers=_auth_header(),
    )

    assert response.status_code == 200
    assert response.json()["url"] == "https://checkout.stripe.com/test"
    mock_stripe.checkout.Session.create.assert_called_once()
    call_args = mock_stripe.checkout.Session.create.call_args
    assert call_args.kwargs["line_items"][0]["price"] == "price_monthly_test"


@patch("chat_recall_api.routers.billing.stripe")
def test_checkout_annual(mock_stripe, client_with_mocks):
    client, conn, settings = client_with_mocks

    # Existing customer
    mock_cur_sub = AsyncMock()
    mock_cur_sub.fetchone = AsyncMock(return_value={"stripe_customer_id": "cus_existing"})
    conn.execute = AsyncMock(return_value=mock_cur_sub)

    mock_stripe.checkout.Session.create.return_value = MagicMock(
        url="https://checkout.stripe.com/annual"
    )

    response = client.post(
        "/billing/checkout",
        json={"plan": "annual"},
        headers=_auth_header(),
    )

    assert response.status_code == 200
    call_args = mock_stripe.checkout.Session.create.call_args
    assert call_args.kwargs["line_items"][0]["price"] == "price_annual_test"
    assert call_args.kwargs["customer"] == "cus_existing"


def test_checkout_unauthorized(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.post("/billing/checkout", json={"plan": "monthly"})
    assert response.status_code == 401


# ── GET /billing/status ──────────────────────────────────────────────────


def test_billing_status_with_subscription(client_with_mocks):
    client, conn, settings = client_with_mocks

    call_count = 0
    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
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
                "stripe_subscription_id": "sub_test",
            })
        return m

    conn.execute = mock_execute

    response = client.get("/billing/status", headers=_auth_header())

    assert response.status_code == 200
    data = response.json()
    assert data["subscription_status"] == "active"
    assert data["plan"] == "pro"
    assert data["stripe_subscription_id"] == "sub_test"


def test_billing_status_no_subscription(client_with_mocks):
    client, conn, settings = client_with_mocks

    call_count = 0
    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if call_count == 1:
            m.fetchone = AsyncMock(return_value={
                "trial_ends_at": "2024-01-15",
                "subscription_status": "trial",
            })
        else:
            m.fetchone = AsyncMock(return_value=None)
        return m

    conn.execute = mock_execute

    response = client.get("/billing/status", headers=_auth_header())

    assert response.status_code == 200
    data = response.json()
    assert data["subscription_status"] == "trial"
    assert data["plan"] is None


def test_billing_status_user_not_found(client_with_mocks):
    client, conn, settings = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.get("/billing/status", headers=_auth_header())
    assert response.status_code == 404


def test_billing_status_unauthorized(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.get("/billing/status")
    assert response.status_code == 401


# ── POST /billing/cancel ─────────────────────────────────────────────────


@patch("chat_recall_api.routers.billing.stripe")
def test_cancel_subscription(mock_stripe, client_with_mocks):
    client, conn, settings = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value={
        "stripe_subscription_id": "sub_cancel_me"
    })
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.post("/billing/cancel", headers=_auth_header())

    assert response.status_code == 200
    assert response.json()["cancelled"] is True
    mock_stripe.Subscription.modify.assert_called_once_with(
        "sub_cancel_me", cancel_at_period_end=True
    )


def test_cancel_no_subscription(client_with_mocks):
    client, conn, settings = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.post("/billing/cancel", headers=_auth_header())
    assert response.status_code == 400


def test_cancel_unauthorized(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.post("/billing/cancel")
    assert response.status_code == 401


# ── POST /webhooks/stripe ────────────────────────────────────────────────


def test_webhook_checkout_completed(client_with_mocks):
    client, conn, settings = client_with_mocks

    conn.execute = AsyncMock()

    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {"user_id": "user-uuid"},
                "customer": "cus_test",
                "subscription": "sub_test",
            }
        },
    }

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(event),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["received"] is True
    # Verify upsert was called (INSERT INTO subscriptions + UPDATE users + commit)
    assert conn.execute.call_count >= 2


def test_webhook_invoice_paid(client_with_mocks):
    client, conn, settings = client_with_mocks

    conn.execute = AsyncMock()

    event = {
        "type": "invoice.paid",
        "data": {
            "object": {
                "subscription": "sub_test",
                "lines": {
                    "data": [{"period": {"end": 1700000000}}]
                },
            }
        },
    }

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(event),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert conn.execute.call_count >= 2


def test_webhook_payment_failed(client_with_mocks):
    client, conn, settings = client_with_mocks

    conn.execute = AsyncMock()

    event = {
        "type": "invoice.payment_failed",
        "data": {
            "object": {"subscription": "sub_test"},
        },
    }

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(event),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert conn.execute.call_count >= 2


def test_webhook_subscription_deleted(client_with_mocks):
    client, conn, settings = client_with_mocks

    conn.execute = AsyncMock()

    event = {
        "type": "customer.subscription.deleted",
        "data": {
            "object": {"id": "sub_test"},
        },
    }

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(event),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert conn.execute.call_count >= 2


def test_webhook_unhandled_event(client_with_mocks):
    client, conn, settings = client_with_mocks

    event = {
        "type": "some.other.event",
        "data": {"object": {}},
    }

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(event),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["received"] is True


@patch("chat_recall_api.routers.billing.stripe")
def test_webhook_with_signature_verification(mock_stripe, client_with_mocks):
    client, conn, settings = client_with_mocks

    # Enable webhook secret for this test
    settings.stripe_webhook_secret = "whsec_test"

    mock_stripe.Webhook.construct_event.return_value = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {"user_id": "user-uuid"},
                "customer": "cus_test",
                "subscription": "sub_test",
            }
        },
    }
    conn.execute = AsyncMock()

    response = client.post(
        "/webhooks/stripe",
        content=b'{"type": "checkout.session.completed"}',
        headers={
            "Content-Type": "application/json",
            "stripe-signature": "t=123,v1=abc",
        },
    )

    assert response.status_code == 200
    mock_stripe.Webhook.construct_event.assert_called_once()

    # Reset for other tests
    settings.stripe_webhook_secret = ""


@patch("chat_recall_api.routers.billing.stripe")
def test_webhook_invalid_signature(mock_stripe, client_with_mocks):
    client, conn, settings = client_with_mocks

    settings.stripe_webhook_secret = "whsec_test"

    import stripe as real_stripe
    mock_stripe.SignatureVerificationError = real_stripe.SignatureVerificationError
    mock_stripe.Webhook.construct_event.side_effect = real_stripe.SignatureVerificationError(
        "bad sig", "sig_header"
    )

    response = client.post(
        "/webhooks/stripe",
        content=b'{}',
        headers={
            "Content-Type": "application/json",
            "stripe-signature": "bad",
        },
    )

    assert response.status_code == 400

    settings.stripe_webhook_secret = ""
