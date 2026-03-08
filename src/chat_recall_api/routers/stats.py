"""Stats routes: dashboard counters, subscription info, recent uploads."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from chat_recall_api.auth import get_current_user
from chat_recall_api.deps import get_db
from chat_recall_api.ratelimit import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()


def _trial_days_remaining(trial_ends_at: Any) -> int | None:
    """Calculate trial days remaining from trial_ends_at.

    Returns:
        int >= 0 if trial_ends_at is set (0 when expired),
        None if trial_ends_at is null.
    """
    if trial_ends_at is None:
        return None

    if isinstance(trial_ends_at, str):
        # Parse date or datetime string
        try:
            trial_ends_at = datetime.fromisoformat(trial_ends_at)
        except ValueError:
            return 0

    if isinstance(trial_ends_at, datetime):
        remaining = (trial_ends_at.date() - date.today()).days
    elif isinstance(trial_ends_at, date):
        remaining = (trial_ends_at - date.today()).days
    else:
        return 0

    return max(remaining, 0)


@router.get("/stats", dependencies=[Depends(rate_limit(60, 60))])
async def get_stats(
    claims: dict = Depends(get_current_user),
    conn: AsyncConnection = Depends(get_db),
) -> dict:
    """Get the current user's dashboard stats.

    Returns cached counters from the users table, a live conversation count,
    and the 5 most recent uploads.
    """
    conn.row_factory = dict_row
    user_id = claims["sub"]

    # Fetch cached counters from users table
    cur = await conn.execute(
        "SELECT total_conversations, total_messages, total_uploads, "
        "last_upload_at, subscription_status, trial_ends_at "
        "FROM users WHERE id = %s",
        (user_id,),
    )
    user = await cur.fetchone()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Live conversation count
    cur = await conn.execute(
        "SELECT COUNT(*) AS cnt FROM conversations WHERE user_id = %s",
        (user_id,),
    )
    row = await cur.fetchone()
    storage_conversations = row["cnt"] if row else 0

    # Recent uploads (last 5)
    cur = await conn.execute(
        "SELECT id, filename, status, conversations_imported, messages_imported, created_at "
        "FROM uploads WHERE user_id = %s ORDER BY created_at DESC LIMIT 5",
        (user_id,),
    )
    upload_rows = await cur.fetchall()

    recent_uploads = [
        {
            "id": str(u["id"]),
            "filename": u.get("filename"),
            "status": u.get("status"),
            "conversations_imported": u.get("conversations_imported"),
            "messages_imported": u.get("messages_imported"),
            "created_at": str(u["created_at"]) if u.get("created_at") else None,
        }
        for u in upload_rows
    ]

    return {
        "total_conversations": user.get("total_conversations", 0),
        "total_messages": user.get("total_messages", 0),
        "total_uploads": user.get("total_uploads", 0),
        "last_upload_at": str(user["last_upload_at"]) if user.get("last_upload_at") else None,
        "subscription_status": user.get("subscription_status"),
        "trial_ends_at": str(user["trial_ends_at"]) if user.get("trial_ends_at") else None,
        "trial_days_remaining": _trial_days_remaining(user.get("trial_ends_at")),
        "storage_conversations": storage_conversations,
        "recent_uploads": recent_uploads,
    }
