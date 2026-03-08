"""Subscription / trial access control.

Provides a FastAPI dependency that gates routes behind an active trial
or paid subscription.  Apply to any route with:

    @router.get("/protected", dependencies=[Depends(require_active_access)])

Or inject the access info:

    async def my_route(access: AccessInfo = Depends(require_active_access)):
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from chat_recall_api.auth import get_current_user
from chat_recall_api.deps import get_db

logger = logging.getLogger(__name__)


@dataclass
class AccessInfo:
    """Describes why the user has (or lacks) access."""

    user_id: str
    allowed: bool
    reason: str  # "trial", "subscription", "past_due", "cancelled_grace"
    trial_ends_at: datetime | None = None
    current_period_end: datetime | None = None


async def require_active_access(
    claims: dict = Depends(get_current_user),
    conn: AsyncConnection = Depends(get_db),
) -> AccessInfo:
    """FastAPI dependency: require an active trial or subscription.

    Access is granted when any of these hold:
    1. subscription_status == 'active'
    2. subscription_status == 'trial' AND trial_ends_at > now
    3. subscription_status == 'past_due' (Stripe is retrying payment)
    4. subscription_status == 'cancelled' AND subscription.current_period_end > now
       (user cancelled but billing period hasn't ended yet)

    Raises HTTPException 402 otherwise.
    """
    conn.row_factory = dict_row
    user_id = claims["sub"]
    now = datetime.now(timezone.utc)

    cur = await conn.execute(
        "SELECT subscription_status, trial_ends_at FROM users WHERE id = %s",
        (user_id,),
    )
    user = await cur.fetchone()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    sub_status = user.get("subscription_status") or "none"
    trial_ends_at = user.get("trial_ends_at")

    # 1. Active subscription
    if sub_status == "active":
        return AccessInfo(
            user_id=user_id,
            allowed=True,
            reason="subscription",
        )

    # 2. Active trial
    if sub_status == "trial" and trial_ends_at:
        if _is_future(trial_ends_at, now):
            return AccessInfo(
                user_id=user_id,
                allowed=True,
                reason="trial",
                trial_ends_at=trial_ends_at if isinstance(trial_ends_at, datetime) else None,
            )

    # 3. Past due — Stripe is retrying, keep access
    if sub_status == "past_due":
        return AccessInfo(
            user_id=user_id,
            allowed=True,
            reason="past_due",
        )

    # 4. Cancelled but still within billing period
    if sub_status == "cancelled":
        cur = await conn.execute(
            "SELECT current_period_end FROM subscriptions WHERE user_id = %s",
            (user_id,),
        )
        sub = await cur.fetchone()
        if sub and sub.get("current_period_end"):
            period_end = sub["current_period_end"]
            if _is_future(period_end, now):
                return AccessInfo(
                    user_id=user_id,
                    allowed=True,
                    reason="cancelled_grace",
                    current_period_end=period_end if isinstance(period_end, datetime) else None,
                )

    # No access
    if sub_status == "trial":
        detail = "Your free trial has expired. Subscribe to continue."
    elif sub_status == "cancelled":
        detail = "Your subscription has ended. Resubscribe to regain access."
    else:
        detail = "A subscription is required to access this feature."

    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=detail,
    )


def _is_future(dt: object, now: datetime) -> bool:
    """Check if a datetime (or stringified datetime) is in the future."""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > now
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed > now
        except (ValueError, TypeError):
            return False
    return False
