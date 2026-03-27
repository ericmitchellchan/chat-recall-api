"""User management routes: sync, profile, update."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import stripe
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from chat_recall_api.auth import get_current_user, verify_internal_key
from chat_recall_api.config import Settings, get_settings
from chat_recall_api.deps import get_db
from chat_recall_api.schemas.user import UserResponse, UserSync, UserUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


def _format_user(row: dict[str, Any]) -> UserResponse:
    """Convert a database row to UserResponse."""
    return UserResponse(
        id=str(row["id"]),
        email=row["email"],
        name=row.get("name"),
        github_id=row.get("github_id"),
        google_id=row.get("google_id"),
        avatar_url=row.get("avatar_url"),
        created_at=str(row["created_at"]) if row.get("created_at") else None,
        updated_at=str(row["updated_at"]) if row.get("updated_at") else None,
    )


@router.post(
    "/auth/sync-user",
    response_model=UserResponse,
    dependencies=[Depends(verify_internal_key)],
)
async def sync_user(
    body: UserSync,
    conn: AsyncConnection = Depends(get_db),
) -> UserResponse:
    """Upsert a user from OAuth callback. Idempotent.

    Lookup order: github_id → google_id → email.
    If found, returns existing user. If not, creates new user.
    """
    conn.row_factory = dict_row

    # Try github_id
    if body.github_id:
        cur = await conn.execute(
            "SELECT * FROM users WHERE github_id = %s", (body.github_id,)
        )
        user = await cur.fetchone()
        if user:
            return _format_user(user)

    # Try google_id
    if body.google_id:
        cur = await conn.execute(
            "SELECT * FROM users WHERE google_id = %s", (body.google_id,)
        )
        user = await cur.fetchone()
        if user:
            return _format_user(user)

    # Try email
    cur = await conn.execute(
        "SELECT * FROM users WHERE email = %s", (body.email,)
    )
    user = await cur.fetchone()
    if user:
        # Link identity if missing
        updates = {}
        if body.github_id and not user.get("github_id"):
            updates["github_id"] = body.github_id
        if body.google_id and not user.get("google_id"):
            updates["google_id"] = body.google_id
        if body.avatar_url and not user.get("avatar_url"):
            updates["avatar_url"] = body.avatar_url

        if updates:
            set_parts = [f"{k} = %s" for k in updates]
            values = list(updates.values()) + [user["id"]]
            await conn.execute(
                f"UPDATE users SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = %s",
                values,
            )
            await conn.commit()
            # Re-fetch
            cur = await conn.execute("SELECT * FROM users WHERE id = %s", (user["id"],))
            user = await cur.fetchone()

        return _format_user(user)

    # Create new user with 14-day trial
    user_id = str(uuid.uuid4())
    trial_ends_at = datetime.now(timezone.utc) + timedelta(days=14)
    cur = await conn.execute(
        "INSERT INTO users (id, email, name, github_id, google_id, avatar_url, "
        "subscription_status, trial_ends_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'trial', %s) RETURNING *",
        (user_id, body.email, body.name, body.github_id, body.google_id, body.avatar_url, trial_ends_at),
    )
    user = await cur.fetchone()
    await conn.commit()
    return _format_user(user)


@router.get("/users/me", response_model=UserResponse)
async def get_me(
    claims: dict = Depends(get_current_user),
    conn: AsyncConnection = Depends(get_db),
) -> UserResponse:
    """Get current user profile from JWT claims."""
    conn.row_factory = dict_row
    user_id = claims["sub"]

    cur = await conn.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = await cur.fetchone()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return _format_user(user)


@router.delete("/account")
async def delete_account(
    claims: dict = Depends(get_current_user),
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Permanently delete the current user's account and all associated data.

    GDPR/CCPA compliant — deletes all conversations, messages, threads,
    uploads, subscriptions, and the user record itself. Immediate and irreversible.
    """
    conn.row_factory = dict_row
    user_id = claims["sub"]

    # Verify user exists
    cur = await conn.execute("SELECT id FROM users WHERE id = %s", (user_id,))
    user = await cur.fetchone()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Cancel active Stripe subscription before deleting data
    cur = await conn.execute(
        "SELECT stripe_subscription_id FROM subscriptions WHERE user_id = %s",
        (user_id,),
    )
    sub = await cur.fetchone()
    if sub and sub.get("stripe_subscription_id"):
        try:
            stripe.api_key = settings.stripe_secret_key
            stripe.Subscription.cancel(sub["stripe_subscription_id"])
        except Exception as e:
            logger.warning("Failed to cancel Stripe subscription %s: %s", sub["stripe_subscription_id"], e)

    counts: dict[str, int] = {}

    # Delete messages for user's conversations
    cur = await conn.execute(
        "DELETE FROM messages WHERE conversation_id IN "
        "(SELECT id FROM conversations WHERE user_id = %s)",
        (user_id,),
    )
    counts["messages"] = cur.rowcount

    # Delete thread_conversations for user's threads
    cur = await conn.execute(
        "DELETE FROM thread_conversations WHERE thread_id IN "
        "(SELECT id FROM threads WHERE user_id = %s)",
        (user_id,),
    )
    counts["thread_conversations"] = cur.rowcount

    # Delete threads
    cur = await conn.execute("DELETE FROM threads WHERE user_id = %s", (user_id,))
    counts["threads"] = cur.rowcount

    # Delete conversations (also clears search index via tsvector)
    cur = await conn.execute("DELETE FROM conversations WHERE user_id = %s", (user_id,))
    counts["conversations"] = cur.rowcount

    # Delete uploads
    cur = await conn.execute("DELETE FROM uploads WHERE user_id = %s", (user_id,))
    counts["uploads"] = cur.rowcount

    # Delete subscription
    cur = await conn.execute("DELETE FROM subscriptions WHERE user_id = %s", (user_id,))
    counts["subscriptions"] = cur.rowcount

    # Delete the user record
    cur = await conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
    counts["users"] = cur.rowcount

    await conn.commit()

    return {"deleted": True, "counts": counts}


@router.patch("/users/me", response_model=UserResponse)
async def update_me(
    body: UserUpdate,
    claims: dict = Depends(get_current_user),
    conn: AsyncConnection = Depends(get_db),
) -> UserResponse:
    """Update current user's profile (name, avatar)."""
    conn.row_factory = dict_row
    user_id = claims["sub"]

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    set_parts = [f"{k} = %s" for k in updates]
    values = list(updates.values()) + [user_id]
    await conn.execute(
        f"UPDATE users SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = %s",
        values,
    )
    await conn.commit()

    cur = await conn.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = await cur.fetchone()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return _format_user(user)
