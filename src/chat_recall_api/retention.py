"""Data retention cron job.

Enforces the 30-day grace period after trial expiry or subscription
cancellation.  Run daily via cron / scheduler:

    python -m chat_recall_api.retention

Two passes:
1. **Delete** — Users whose grace period expired (30+ days since trial end
   or subscription cancellation).  Runs the full GDPR cascade delete and
   sends account_deleted email.
2. **Warn** — Users who just entered the grace period and haven't been
   warned yet.  Sends grace_period_warning email.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import stripe
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from chat_recall_api.config import get_settings
from chat_recall_api.deps import close_db_pool, init_db_pool, get_db
from chat_recall_api.email.sender import render_template, send_email

logger = logging.getLogger(__name__)


async def _delete_user_data(conn: AsyncConnection, user_id: str) -> dict[str, int]:
    """Run the full GDPR cascade delete for a single user.

    Same logic as DELETE /account but without auth context.
    """
    conn.row_factory = dict_row

    # Cancel Stripe subscription and delete customer before deleting data
    cur = await conn.execute(
        "SELECT stripe_customer_id, stripe_subscription_id FROM subscriptions WHERE user_id = %s",
        (user_id,),
    )
    sub = await cur.fetchone()
    if sub:
        settings = get_settings()
        stripe.api_key = settings.stripe_secret_key
        if sub.get("stripe_subscription_id"):
            try:
                stripe.Subscription.cancel(sub["stripe_subscription_id"])
            except Exception as e:
                logger.warning("Failed to cancel Stripe subscription %s: %s", sub["stripe_subscription_id"], e)
        if sub.get("stripe_customer_id"):
            try:
                stripe.Customer.delete(sub["stripe_customer_id"])
            except Exception as e:
                logger.warning("Failed to delete Stripe customer %s: %s", sub["stripe_customer_id"], e)

    counts: dict[str, int] = {}

    cur = await conn.execute(
        "DELETE FROM messages WHERE conversation_id IN "
        "(SELECT id FROM conversations WHERE user_id = %s)",
        (user_id,),
    )
    counts["messages"] = cur.rowcount

    cur = await conn.execute(
        "DELETE FROM thread_conversations WHERE thread_id IN "
        "(SELECT id FROM threads WHERE user_id = %s)",
        (user_id,),
    )
    counts["thread_conversations"] = cur.rowcount

    cur = await conn.execute("DELETE FROM threads WHERE user_id = %s", (user_id,))
    counts["threads"] = cur.rowcount

    # Delete orphaned sources for user's conversations
    cur = await conn.execute(
        "DELETE FROM sources WHERE id IN "
        "(SELECT DISTINCT source_id FROM conversations WHERE user_id = %s AND source_id IS NOT NULL)",
        (user_id,),
    )
    counts["sources"] = cur.rowcount

    cur = await conn.execute("DELETE FROM conversations WHERE user_id = %s", (user_id,))
    counts["conversations"] = cur.rowcount

    cur = await conn.execute("DELETE FROM uploads WHERE user_id = %s", (user_id,))
    counts["uploads"] = cur.rowcount

    cur = await conn.execute("DELETE FROM subscriptions WHERE user_id = %s", (user_id,))
    counts["subscriptions"] = cur.rowcount

    cur = await conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
    counts["users"] = cur.rowcount

    return counts


async def _process_deletions(conn: AsyncConnection) -> int:
    """Find and delete users past the 30-day grace period.

    Targets:
    - Trial users: trial_ends_at + 30 days < now, no active subscription
    - Cancelled users: cancelled_at + 30 days < now
    """
    conn.row_factory = dict_row
    settings = get_settings()

    # Users whose trial expired 30+ days ago and never subscribed
    cur = await conn.execute(
        "SELECT id, email FROM users "
        "WHERE subscription_status IN ('trial', 'none', 'expired') "
        "AND trial_ends_at IS NOT NULL "
        "AND trial_ends_at + INTERVAL '30 days' < NOW()"
    )
    expired_trial_users = await cur.fetchall()

    # Users whose subscription was cancelled 30+ days ago
    cur = await conn.execute(
        "SELECT id, email FROM users "
        "WHERE subscription_status = 'cancelled' "
        "AND cancelled_at IS NOT NULL "
        "AND cancelled_at + INTERVAL '30 days' < NOW()"
    )
    cancelled_users = await cur.fetchall()

    users_to_delete = expired_trial_users + cancelled_users
    deleted_count = 0

    for user in users_to_delete:
        try:
            counts = await _delete_user_data(conn, user["id"])
            await conn.commit()

            html = render_template("account_deleted.html", {
                "frontend_url": settings.frontend_url,
                "unsubscribe_url": f"{settings.frontend_url}/unsubscribe",
            })
            await send_email(
                to=user["email"],
                subject="Your Chat Recall account has been deleted",
                html_body=html,
            )

            deleted_count += 1
            logger.info(
                "Retention delete: user=%s counts=%s",
                user["id"], counts,
            )
        except Exception:
            logger.exception("Failed to delete user %s", user["id"])
            # Roll back this user's transaction and continue
            await conn.rollback()

    return deleted_count


async def _process_warnings(conn: AsyncConnection) -> int:
    """Send grace period warnings to users who recently lost access.

    Targets users who:
    - Trial expired (trial_ends_at < now) but within 30 days, OR
    - Subscription cancelled (cancelled_at < now) but within 30 days
    AND have not been warned yet (retention_warned_at IS NULL).
    """
    conn.row_factory = dict_row
    settings = get_settings()

    # Trial users in grace period, not yet warned
    cur = await conn.execute(
        "SELECT id, email, trial_ends_at FROM users "
        "WHERE subscription_status IN ('trial', 'none', 'expired') "
        "AND trial_ends_at IS NOT NULL "
        "AND trial_ends_at < NOW() "
        "AND trial_ends_at + INTERVAL '30 days' >= NOW() "
        "AND retention_warned_at IS NULL"
    )
    trial_users = await cur.fetchall()

    # Cancelled users in grace period, not yet warned
    cur = await conn.execute(
        "SELECT id, email, cancelled_at FROM users "
        "WHERE subscription_status = 'cancelled' "
        "AND cancelled_at IS NOT NULL "
        "AND cancelled_at < NOW() "
        "AND cancelled_at + INTERVAL '30 days' >= NOW() "
        "AND retention_warned_at IS NULL"
    )
    cancelled_users = await cur.fetchall()

    warned_count = 0

    for user in trial_users + cancelled_users:
        try:
            # Calculate deletion date (30 days from expiry/cancellation)
            expiry = user.get("trial_ends_at") or user.get("cancelled_at")
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if isinstance(expiry, datetime):
                from datetime import timedelta
                deletion_date = (expiry + timedelta(days=30)).strftime("%B %d, %Y")
            else:
                deletion_date = "30 days from now"

            html = render_template("grace_period_warning.html", {
                "deletion_date": deletion_date,
                "frontend_url": settings.frontend_url,
                "unsubscribe_url": f"{settings.frontend_url}/unsubscribe",
            })
            await send_email(
                to=user["email"],
                subject="Your Chat Recall data will be deleted soon",
                html_body=html,
            )

            # Mark as warned so we don't re-send
            await conn.execute(
                "UPDATE users SET retention_warned_at = NOW() WHERE id = %s",
                (user["id"],),
            )
            await conn.commit()

            warned_count += 1
            logger.info("Retention warning sent: user=%s", user["id"])
        except Exception:
            logger.exception("Failed to warn user %s", user["id"])
            await conn.rollback()

    return warned_count


async def run_retention() -> dict[str, int]:
    """Run the full retention job. Returns counts."""
    settings = get_settings()
    await init_db_pool(settings.database_url)

    # Get a connection from the pool
    async for conn in get_db():
        deleted = await _process_deletions(conn)
        warned = await _process_warnings(conn)

    await close_db_pool()

    results = {"deleted": deleted, "warned": warned}
    logger.info("Retention job complete: %s", results)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_retention())
