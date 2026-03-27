"""Billing routes: Stripe checkout, subscription management, webhooks."""

from __future__ import annotations

import logging
from typing import Any

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from chat_recall_api.auth import get_current_user
from chat_recall_api.config import Settings, get_settings
from chat_recall_api.deps import get_db
from chat_recall_api.ratelimit import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()


def _init_stripe(settings: Settings) -> None:
    """Configure the stripe module with our secret key."""
    stripe.api_key = settings.stripe_secret_key


# ── Checkout ────────────────────────────────────────────────────────────


@router.post("/billing/checkout", dependencies=[Depends(rate_limit(10, 3600))])
async def create_checkout_session(
    body: dict,
    claims: dict = Depends(get_current_user),
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Create a Stripe Checkout session for the user to subscribe.

    Body: {"plan": "monthly" | "annual"}
    Returns: {"url": "https://checkout.stripe.com/..."}
    """
    _init_stripe(settings)
    conn.row_factory = dict_row
    user_id = claims["sub"]

    plan = body.get("plan", "monthly")
    if plan == "annual":
        price_id = settings.stripe_annual_price_id
    else:
        price_id = settings.stripe_monthly_price_id

    if not price_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe price not configured",
        )

    # Get or create Stripe customer
    cur = await conn.execute(
        "SELECT stripe_customer_id FROM subscriptions WHERE user_id = %s",
        (user_id,),
    )
    row = await cur.fetchone()
    customer_id = row["stripe_customer_id"] if row else None

    if not customer_id:
        # Get user email for Stripe customer
        cur = await conn.execute("SELECT email FROM users WHERE id = %s", (user_id,))
        user = await cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        customer = stripe.Customer.create(
            email=user["email"],
            metadata={"user_id": user_id},
        )
        customer_id = customer.id

    session = stripe.checkout.Session.create(
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{settings.frontend_url}/dashboard?subscribed=true",
        cancel_url=f"{settings.frontend_url}/dashboard",
        metadata={"user_id": user_id},
    )

    return {"url": session.url}


# ── Subscription status ─────────────────────────────────────────────────


@router.get("/billing/status")
async def get_billing_status(
    claims: dict = Depends(get_current_user),
    conn: AsyncConnection = Depends(get_db),
) -> dict:
    """Get the current user's billing/subscription status."""
    conn.row_factory = dict_row
    user_id = claims["sub"]

    # Get user for trial info
    cur = await conn.execute(
        "SELECT trial_ends_at, subscription_status FROM users WHERE id = %s",
        (user_id,),
    )
    user = await cur.fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get subscription details
    cur = await conn.execute(
        "SELECT plan, status, current_period_end, stripe_subscription_id "
        "FROM subscriptions WHERE user_id = %s",
        (user_id,),
    )
    sub = await cur.fetchone()

    return {
        "subscription_status": user.get("subscription_status", "none"),
        "trial_ends_at": str(user["trial_ends_at"]) if user.get("trial_ends_at") else None,
        "plan": sub["plan"] if sub else None,
        "current_period_end": str(sub["current_period_end"]) if sub and sub.get("current_period_end") else None,
        "stripe_subscription_id": sub["stripe_subscription_id"] if sub else None,
    }


# ── Cancel subscription ─────────────────────────────────────────────────


@router.post("/billing/cancel", dependencies=[Depends(rate_limit(10, 3600))])
async def cancel_subscription(
    claims: dict = Depends(get_current_user),
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Cancel the current user's subscription at period end."""
    _init_stripe(settings)
    conn.row_factory = dict_row
    user_id = claims["sub"]

    cur = await conn.execute(
        "SELECT stripe_subscription_id FROM subscriptions WHERE user_id = %s",
        (user_id,),
    )
    sub = await cur.fetchone()
    if not sub or not sub.get("stripe_subscription_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active subscription found",
        )

    # Cancel at period end (user keeps access until billing cycle ends)
    stripe.Subscription.modify(
        sub["stripe_subscription_id"],
        cancel_at_period_end=True,
    )

    return {"cancelled": True, "message": "Subscription will cancel at end of billing period"}


# ── Stripe Webhooks ──────────────────────────────────────────────────────


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Handle Stripe webhook events.

    Events handled:
    - checkout.session.completed → activate subscription
    - invoice.paid → confirm renewal
    - invoice.payment_failed → mark past_due
    - customer.subscription.deleted → mark cancelled
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not settings.stripe_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret not configured",
        )

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret,
        )
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        logger.error("Webhook error: %s", e)
        raise HTTPException(status_code=400, detail="Webhook error")

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})
    conn.row_factory = dict_row

    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(conn, data)

    elif event_type == "invoice.paid":
        await _handle_invoice_paid(conn, data)

    elif event_type == "invoice.payment_failed":
        await _handle_payment_failed(conn, data)

    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(conn, data)

    else:
        logger.info("Unhandled webhook event: %s", event_type)

    return {"received": True}


async def _handle_checkout_completed(conn: AsyncConnection, data: dict) -> None:
    """Activate subscription after successful checkout."""
    user_id = data.get("metadata", {}).get("user_id")
    customer_id = data.get("customer")
    subscription_id = data.get("subscription")

    if not user_id:
        logger.warning("checkout.session.completed missing user_id in metadata")
        return

    # Upsert subscription record
    await conn.execute(
        "INSERT INTO subscriptions (user_id, stripe_customer_id, stripe_subscription_id, plan, status) "
        "VALUES (%s, %s, %s, 'pro', 'active') "
        "ON CONFLICT (user_id) DO UPDATE SET "
        "stripe_customer_id = EXCLUDED.stripe_customer_id, "
        "stripe_subscription_id = EXCLUDED.stripe_subscription_id, "
        "status = 'active', plan = 'pro'",
        (user_id, customer_id, subscription_id),
    )

    # Update user status
    await conn.execute(
        "UPDATE users SET subscription_status = 'active', updated_at = NOW() WHERE id = %s",
        (user_id,),
    )
    await conn.commit()
    logger.info("Subscription activated for user %s", user_id)


async def _handle_invoice_paid(conn: AsyncConnection, data: dict) -> None:
    """Confirm subscription renewal."""
    subscription_id = data.get("subscription")
    if not subscription_id:
        return

    period_end = data.get("lines", {}).get("data", [{}])[0].get("period", {}).get("end")

    await conn.execute(
        "UPDATE subscriptions SET status = 'active', current_period_end = to_timestamp(%s) "
        "WHERE stripe_subscription_id = %s",
        (period_end, subscription_id),
    )

    # Also update user status in case it was past_due
    await conn.execute(
        "UPDATE users SET subscription_status = 'active', updated_at = NOW() "
        "WHERE id = (SELECT user_id FROM subscriptions WHERE stripe_subscription_id = %s)",
        (subscription_id,),
    )
    await conn.commit()


async def _handle_payment_failed(conn: AsyncConnection, data: dict) -> None:
    """Mark subscription as past_due on payment failure."""
    subscription_id = data.get("subscription")
    if not subscription_id:
        return

    await conn.execute(
        "UPDATE subscriptions SET status = 'past_due' WHERE stripe_subscription_id = %s",
        (subscription_id,),
    )
    await conn.execute(
        "UPDATE users SET subscription_status = 'past_due', updated_at = NOW() "
        "WHERE id = (SELECT user_id FROM subscriptions WHERE stripe_subscription_id = %s)",
        (subscription_id,),
    )
    await conn.commit()
    logger.warning("Payment failed for subscription %s", subscription_id)


async def _handle_subscription_deleted(conn: AsyncConnection, data: dict) -> None:
    """Mark subscription as cancelled."""
    subscription_id = data.get("id")
    if not subscription_id:
        return

    await conn.execute(
        "UPDATE subscriptions SET status = 'cancelled' WHERE stripe_subscription_id = %s",
        (subscription_id,),
    )
    await conn.execute(
        "UPDATE users SET subscription_status = 'cancelled', cancelled_at = NOW(), updated_at = NOW() "
        "WHERE id = (SELECT user_id FROM subscriptions WHERE stripe_subscription_id = %s)",
        (subscription_id,),
    )
    await conn.commit()
    logger.info("Subscription cancelled: %s", subscription_id)
