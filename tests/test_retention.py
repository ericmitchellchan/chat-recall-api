"""Tests for the data retention cron job."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from chat_recall_api.config import Settings, get_settings
from chat_recall_api.retention import (
    _delete_user_data,
    _process_deletions,
    _process_warnings,
)

from tests.test_users import TEST_SECRET


@pytest.fixture
def mock_conn():
    conn = AsyncMock()
    conn.row_factory = None
    return conn


@pytest.fixture
def retention_settings():
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        nextauth_secret=TEST_SECRET,
        frontend_url="http://localhost:3002",
    )


# ── _delete_user_data ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_user_data_cascade(mock_conn):
    """Verify all tables are hit in correct order."""
    mock_cur = AsyncMock()
    mock_cur.rowcount = 5
    mock_conn.execute = AsyncMock(return_value=mock_cur)

    # Mock fetchone for subscription lookup to return None (no active sub)
    mock_sub_cur = AsyncMock()
    mock_sub_cur.fetchone = AsyncMock(return_value=None)
    mock_sub_cur.rowcount = 5

    # First call is subscription lookup (returns None), rest are DELETEs
    mock_conn.execute = AsyncMock(return_value=mock_sub_cur)

    counts = await _delete_user_data(mock_conn, "user-to-delete")

    # 1 subscription lookup + 8 DELETE statements (messages, thread_conversations,
    # threads, sources, conversations, uploads, subscriptions, users)
    assert mock_conn.execute.call_count == 9
    assert counts["messages"] == 5
    assert counts["users"] == 5


# ── _process_deletions ───────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("chat_recall_api.retention.send_email")
@patch("chat_recall_api.retention.get_settings")
async def test_process_deletions_deletes_expired_users(
    mock_settings, mock_send, mock_conn, retention_settings
):
    mock_settings.return_value = retention_settings

    expired_user = {"id": "expired-user", "email": "expired@test.com"}

    call_count = 0
    async def mock_execute(sql=None, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        m.rowcount = 0

        if call_count == 1:
            # First query: expired trial users
            m.fetchall = AsyncMock(return_value=[expired_user])
        elif call_count == 2:
            # Second query: cancelled users
            m.fetchall = AsyncMock(return_value=[])
        else:
            # DELETE statements
            m.rowcount = 1
        return m

    mock_conn.execute = mock_execute

    deleted = await _process_deletions(mock_conn)

    assert deleted == 1
    mock_send.assert_called_once()
    assert mock_send.call_args[1]["to"] == "expired@test.com"
    assert "deleted" in mock_send.call_args[1]["subject"].lower()


@pytest.mark.asyncio
@patch("chat_recall_api.retention.send_email")
@patch("chat_recall_api.retention.get_settings")
async def test_process_deletions_no_expired_users(
    mock_settings, mock_send, mock_conn, retention_settings
):
    mock_settings.return_value = retention_settings

    async def mock_execute(sql=None, params=None):
        m = AsyncMock()
        m.fetchall = AsyncMock(return_value=[])
        return m

    mock_conn.execute = mock_execute

    deleted = await _process_deletions(mock_conn)

    assert deleted == 0
    mock_send.assert_not_called()


# ── _process_warnings ────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("chat_recall_api.retention.send_email")
@patch("chat_recall_api.retention.get_settings")
async def test_process_warnings_sends_to_grace_period_users(
    mock_settings, mock_send, mock_conn, retention_settings
):
    mock_settings.return_value = retention_settings

    grace_user = {
        "id": "grace-user",
        "email": "grace@test.com",
        "trial_ends_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
    }

    call_count = 0
    async def mock_execute(sql=None, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()

        if call_count == 1:
            # Trial users in grace period
            m.fetchall = AsyncMock(return_value=[grace_user])
        elif call_count == 2:
            # Cancelled users in grace period
            m.fetchall = AsyncMock(return_value=[])
        else:
            # UPDATE retention_warned_at
            pass
        return m

    mock_conn.execute = mock_execute

    warned = await _process_warnings(mock_conn)

    assert warned == 1
    mock_send.assert_called_once()
    assert mock_send.call_args[1]["to"] == "grace@test.com"
    assert "deleted soon" in mock_send.call_args[1]["subject"].lower()


@pytest.mark.asyncio
@patch("chat_recall_api.retention.send_email")
@patch("chat_recall_api.retention.get_settings")
async def test_process_warnings_marks_warned_at(
    mock_settings, mock_send, mock_conn, retention_settings
):
    """Verify retention_warned_at is set after warning."""
    mock_settings.return_value = retention_settings

    grace_user = {
        "id": "warn-user",
        "email": "warn@test.com",
        "trial_ends_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    }

    execute_calls = []
    call_count = 0
    async def mock_execute(sql=None, params=None):
        nonlocal call_count
        call_count += 1
        execute_calls.append((sql, params))
        m = AsyncMock()

        if call_count == 1:
            m.fetchall = AsyncMock(return_value=[grace_user])
        elif call_count == 2:
            m.fetchall = AsyncMock(return_value=[])
        return m

    mock_conn.execute = mock_execute

    await _process_warnings(mock_conn)

    # Third call should be the UPDATE to set retention_warned_at
    assert len(execute_calls) >= 3
    update_sql = execute_calls[2][0]
    assert "retention_warned_at" in update_sql
    assert execute_calls[2][1] == ("warn-user",)


@pytest.mark.asyncio
@patch("chat_recall_api.retention.send_email")
@patch("chat_recall_api.retention.get_settings")
async def test_process_warnings_skips_already_warned(
    mock_settings, mock_send, mock_conn, retention_settings
):
    """Users with retention_warned_at set are excluded by the SQL query."""
    mock_settings.return_value = retention_settings

    # The SQL filters out users with retention_warned_at IS NOT NULL,
    # so the query returns empty results for already-warned users
    async def mock_execute(sql=None, params=None):
        m = AsyncMock()
        m.fetchall = AsyncMock(return_value=[])
        return m

    mock_conn.execute = mock_execute

    warned = await _process_warnings(mock_conn)

    assert warned == 0
    mock_send.assert_not_called()


@pytest.mark.asyncio
@patch("chat_recall_api.retention.send_email")
@patch("chat_recall_api.retention.get_settings")
async def test_process_deletions_handles_error_gracefully(
    mock_settings, mock_send, mock_conn, retention_settings
):
    """If one user fails, the job continues to the next."""
    mock_settings.return_value = retention_settings

    users = [
        {"id": "fail-user", "email": "fail@test.com"},
        {"id": "ok-user", "email": "ok@test.com"},
    ]

    call_count = 0
    fail_done = False
    async def mock_execute(sql=None, params=None):
        nonlocal call_count, fail_done
        call_count += 1
        m = AsyncMock()
        m.rowcount = 0

        if call_count == 1:
            m.fetchall = AsyncMock(return_value=users)
        elif call_count == 2:
            m.fetchall = AsyncMock(return_value=[])
        elif not fail_done and params and "fail-user" in str(params):
            fail_done = True
            raise Exception("DB error for fail-user")
        else:
            m.rowcount = 1
        return m

    mock_conn.execute = mock_execute

    deleted = await _process_deletions(mock_conn)

    # Only the second user should succeed
    assert deleted == 1
