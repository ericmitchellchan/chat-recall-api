"""ChatGPT export importer — parses JSON and inserts into Postgres.

Replicates the import logic from chat-recall-prod but uses psycopg
directly (no SQLAlchemy).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg import AsyncConnection

from chat_recall_api.content import extract_text

logger = logging.getLogger(__name__)


async def import_chatgpt_data(
    conn: AsyncConnection,
    user_id: str,
    conversations_data: list[dict[str, Any]],
    file_path: str = "upload",
) -> dict[str, Any]:
    """Import a list of ChatGPT conversation dicts into Postgres.

    Returns a summary dict with counts of imported/skipped/updated items.
    """
    # Create source record
    cur = await conn.execute(
        "INSERT INTO sources (source_type, file_path, record_count) "
        "VALUES (%s, %s, %s) RETURNING id",
        ("chatgpt", file_path, len(conversations_data)),
    )
    row = await cur.fetchone()
    source_id = row[0]

    total_conversations = 0
    total_messages = 0
    updated_convos = 0
    skipped_convos = 0
    skip_reasons: dict[str, int] = {"already_exists": 0, "missing_id": 0, "parse_error": 0}
    errors: list[str] = []

    for conv in conversations_data:
        conv_id = conv.get("id")
        if not conv_id:
            skipped_convos += 1
            skip_reasons["missing_id"] += 1
            continue

        try:
            messages, has_branches = _parse_conversation(conv, conv_id)
        except Exception as e:
            skipped_convos += 1
            skip_reasons["parse_error"] += 1
            errors.append(f"Parse error for {conv_id}: {e}")
            continue

        # Check if conversation already exists for this user
        cur = await conn.execute(
            "SELECT message_count FROM conversations WHERE id = %s AND user_id = %s",
            (conv_id, user_id),
        )
        existing = await cur.fetchone()

        if existing:
            existing_count = existing[0] if existing else 0
            if len(messages) > existing_count:
                # Re-import: more messages than before
                await conn.execute(
                    "DELETE FROM messages WHERE conversation_id = %s",
                    (conv_id,),
                )
                await _insert_messages_batch(conn, messages)
                await conn.execute(
                    "UPDATE conversations SET message_count = %s, update_time = %s "
                    "WHERE id = %s AND user_id = %s",
                    (len(messages), conv.get("update_time"), conv_id, user_id),
                )
                total_messages += len(messages)
                updated_convos += 1
            else:
                skipped_convos += 1
                skip_reasons["already_exists"] += 1
            continue

        # Determine model
        model = conv.get("default_model_slug")
        if not model:
            model = _find_model_in_messages(conv.get("mapping", {}))

        # Build conversation metadata
        metadata = {
            k: v for k, v in conv.items()
            if k in ("conversation_template_id", "is_archived", "safe_urls") and v is not None
        } or None

        # Insert conversation
        await conn.execute(
            "INSERT INTO conversations "
            "(id, user_id, source_id, title, create_time, update_time, model, gizmo_id, "
            "message_count, has_branches, metadata, source_type) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (
                conv_id, user_id, source_id, conv.get("title"),
                conv.get("create_time"), conv.get("update_time"),
                model, conv.get("gizmo_id"),
                len(messages), has_branches,
                json.dumps(metadata) if metadata else None,
                "chatgpt",
            ),
        )

        # Insert messages
        await _insert_messages_batch(conn, messages)

        total_conversations += 1
        total_messages += len(messages)

    # Update user analytics counters
    if total_conversations or total_messages:
        await conn.execute(
            "UPDATE users SET "
            "total_conversations = total_conversations + %s, "
            "total_messages = total_messages + %s, "
            "total_uploads = total_uploads + 1, "
            "last_upload_at = NOW(), "
            "updated_at = NOW() "
            "WHERE id = %s",
            (total_conversations, total_messages, user_id),
        )

    await conn.commit()

    return {
        "source_id": source_id,
        "conversations_imported": total_conversations,
        "conversations_updated": updated_convos,
        "conversations_skipped": skipped_convos,
        "messages_imported": total_messages,
        "skip_reasons": skip_reasons,
        "errors": errors,
    }


def _parse_conversation(
    conv: dict[str, Any], conv_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Parse a ChatGPT conversation into a list of message dicts.

    Returns (messages, has_branches).
    """
    mapping = conv.get("mapping", {})
    if not mapping:
        return [], False

    # Determine canonical path
    current_node = conv.get("current_node")
    canonical_ids = _trace_canonical_path(mapping, current_node)

    # Detect branches
    has_branches = any(
        len(node.get("children", [])) > 1
        for node in mapping.values()
    )

    messages = []
    for node_id, node in mapping.items():
        msg = node.get("message")
        if not msg:
            continue

        role = msg.get("author", {}).get("role")
        if not role:
            continue

        # Skip system messages with no content
        content = msg.get("content")
        content_type = content.get("content_type", "") if content else ""
        content_text = extract_text(content)

        if role == "system" and not content_text:
            continue

        # Build message metadata
        msg_meta = msg.get("metadata", {})
        msg_metadata = {}
        if msg_meta.get("model_slug"):
            msg_metadata["model_slug"] = msg_meta["model_slug"]
        if msg_meta.get("finish_details"):
            msg_metadata["finish_details"] = msg_meta["finish_details"]

        messages.append({
            "id": msg.get("id", node_id),
            "conversation_id": conv_id,
            "parent_id": node.get("parent"),
            "role": role,
            "content_type": content_type,
            "content_text": content_text,
            "raw_content": json.dumps(content) if content else None,
            "is_canonical": node_id in canonical_ids,
            "create_time": msg.get("create_time"),
            "attachments": msg_meta.get("attachments"),
            "metadata": msg_metadata or None,
        })

    return messages, has_branches


def _trace_canonical_path(
    mapping: dict[str, Any], current_node: str | None,
) -> set[str]:
    """Walk from current_node back to root to find the canonical message path."""
    canonical: set[str] = set()
    if not current_node:
        return canonical
    node_id: str | None = current_node
    while node_id:
        canonical.add(node_id)
        node = mapping.get(node_id)
        if not node:
            break
        node_id = node.get("parent")
    return canonical


def _find_model_in_messages(mapping: dict[str, Any]) -> str | None:
    """Find the model slug from message metadata (fallback)."""
    for node in mapping.values():
        msg = node.get("message")
        if msg and msg.get("metadata", {}).get("model_slug"):
            return msg["metadata"]["model_slug"]
    return None


async def _insert_messages_batch(
    conn: AsyncConnection, messages: list[dict[str, Any]],
) -> int:
    """Insert a batch of messages. Returns count inserted."""
    if not messages:
        return 0
    for msg in messages:
        await conn.execute(
            "INSERT INTO messages "
            "(id, conversation_id, parent_id, role, content_type, content_text, "
            "raw_content, is_canonical, create_time, attachments, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id, conversation_id) DO NOTHING",
            (
                msg["id"], msg["conversation_id"], msg.get("parent_id"),
                msg.get("role"), msg.get("content_type"), msg.get("content_text"),
                msg.get("raw_content"), msg.get("is_canonical", True),
                msg.get("create_time"),
                json.dumps(msg["attachments"]) if msg.get("attachments") else None,
                json.dumps(msg["metadata"]) if msg.get("metadata") else None,
            ),
        )
    return len(messages)
