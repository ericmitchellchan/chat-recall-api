"""Upload routes: accept ChatGPT export files and trigger import."""

from __future__ import annotations

import io
import json
import logging
import uuid
import zipfile

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from chat_recall_api.auth import get_current_user
from chat_recall_api.deps import get_db
from chat_recall_api.importer import import_chatgpt_data
from chat_recall_api.ratelimit import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB
MAX_DECOMPRESSED_SIZE = 1024 * 1024 * 1024  # 1 GB


@router.post("/upload", dependencies=[Depends(rate_limit(5, 3600))])
async def upload_file(
    file: UploadFile,
    claims: dict = Depends(get_current_user),
    conn: AsyncConnection = Depends(get_db),
) -> dict:
    """Upload a ChatGPT export file (JSON or ZIP) for import.

    Accepts:
    - `.json` — raw ChatGPT conversations.json export
    - `.zip` — ChatGPT data export ZIP (extracts conversations.json)

    Returns import results with conversation/message counts.
    """
    conn.row_factory = dict_row
    user_id = claims["sub"]
    filename = file.filename or "upload"

    # Validate file type
    if not filename.endswith((".json", ".zip")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .json and .zip files are accepted",
        )

    # Read file content in chunks to avoid buffering arbitrarily large uploads
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)  # 1 MB chunks
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)} MB",
            )
        chunks.append(chunk)
    content = b"".join(chunks)

    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file",
        )

    # Extract conversations JSON
    try:
        conversations_data = _extract_conversations(content, filename)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    if not conversations_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No conversations found in the uploaded file",
        )

    # Create upload tracking record
    upload_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO uploads (id, user_id, filename, status) "
        "VALUES (%s, %s, %s, 'processing')",
        (upload_id, user_id, filename),
    )
    await conn.commit()

    # Run import
    try:
        result = await import_chatgpt_data(conn, user_id, conversations_data, filename)

        # Update upload record with results
        await conn.execute(
            "UPDATE uploads SET status = 'completed', "
            "conversations_imported = %s, messages_imported = %s, "
            "completed_at = NOW() WHERE id = %s",
            (
                result["conversations_imported"] + result.get("conversations_updated", 0),
                result["messages_imported"],
                upload_id,
            ),
        )
        await conn.commit()

        return {
            "upload_id": upload_id,
            "filename": filename,
            "status": "completed",
            **result,
        }

    except Exception as e:
        logger.exception("Import failed for upload %s", upload_id)
        await conn.rollback()

        # Mark upload as failed
        await conn.execute(
            "UPDATE uploads SET status = 'failed', error_message = %s, "
            "completed_at = NOW() WHERE id = %s",
            (str(e)[:500], upload_id),
        )
        await conn.commit()

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Import processing failed. Please try again.",
        )


def _extract_conversations(content: bytes, filename: str) -> list[dict]:
    """Extract conversations list from JSON or ZIP content.

    For ZIP files, looks for conversations.json inside the archive.
    """
    if filename.endswith(".zip"):
        return _extract_from_zip(content)
    return _parse_json(content)


def _parse_json(content: bytes) -> list[dict]:
    """Parse JSON content, handling both array and single-conversation formats."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "mapping" in data:
        # Single conversation object
        return [data]
    raise ValueError("Expected a JSON array of conversations or a single conversation object")


def _extract_from_zip(content: bytes) -> list[dict]:
    """Extract conversations.json from a ChatGPT data export ZIP."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # Look for conversations.json (ChatGPT export format)
            candidates = [
                n for n in zf.namelist()
                if n.endswith("conversations.json")
            ]

            if not candidates:
                raise ValueError(
                    "No conversations.json found in ZIP. "
                    "Please upload a ChatGPT data export."
                )

            # Prefer the shortest path (most likely the root one)
            target = min(candidates, key=len)
            info = zf.getinfo(target)
            if info.file_size > MAX_DECOMPRESSED_SIZE:
                raise ValueError(
                    f"conversations.json is too large when decompressed "
                    f"({info.file_size // (1024 * 1024)} MB, max {MAX_DECOMPRESSED_SIZE // (1024 * 1024)} MB)"
                )
            with zf.open(target) as f:
                return _parse_json(f.read())

    except zipfile.BadZipFile:
        raise ValueError("Invalid ZIP file")
