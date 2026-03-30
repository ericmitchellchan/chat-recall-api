"""Tests for user management endpoints and auth middleware."""

import base64
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from chat_recall_api.auth import (
    _b64url_decode,
    _derive_encryption_key,
    decode_nextauth_jwt,
)
from chat_recall_api.main import app


# ── JWE helpers ───────────────────────────────────────────────────────────

TEST_SECRET = "test-nextauth-secret-for-unit-tests"


def _b64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_jwe(payload: dict, secret: str = TEST_SECRET) -> str:
    """Create a JWE token matching NextAuth's format for testing."""
    key = _derive_encryption_key(secret)

    header = {"alg": "dir", "enc": "A256GCM"}
    header_b64 = _b64url_encode(json.dumps(header).encode())
    aad = header_b64.encode("ascii")

    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    plaintext = json.dumps(payload).encode()
    ciphertext_and_tag = aesgcm.encrypt(iv, plaintext, aad)

    # AES-GCM appends 16-byte tag to ciphertext
    ciphertext = ciphertext_and_tag[:-16]
    tag = ciphertext_and_tag[-16:]

    return ".".join([
        header_b64,
        "",  # empty encrypted key for "dir" algorithm
        _b64url_encode(iv),
        _b64url_encode(ciphertext),
        _b64url_encode(tag),
    ])


# ── JWT decryption tests ─────────────────────────────────────────────────


def test_b64url_decode_no_padding():
    encoded = _b64url_encode(b"hello world")
    assert _b64url_decode(encoded) == b"hello world"


def test_derive_encryption_key_deterministic():
    key1 = _derive_encryption_key("my-secret")
    key2 = _derive_encryption_key("my-secret")
    assert key1 == key2
    assert len(key1) == 32


def test_derive_encryption_key_different_secrets():
    key1 = _derive_encryption_key("secret-a")
    key2 = _derive_encryption_key("secret-b")
    assert key1 != key2


def test_decode_nextauth_jwt_roundtrip():
    payload = {"sub": "user-uuid-123", "name": "Test User", "email": "test@example.com"}
    token = _make_jwe(payload)
    decoded = decode_nextauth_jwt(token, TEST_SECRET)
    assert decoded["sub"] == "user-uuid-123"
    assert decoded["name"] == "Test User"
    assert decoded["email"] == "test@example.com"


def test_decode_jwt_wrong_secret():
    payload = {"sub": "user-1"}
    token = _make_jwe(payload, secret=TEST_SECRET)
    with pytest.raises(Exception):
        decode_nextauth_jwt(token, "wrong-secret")


def test_decode_jwt_invalid_format():
    with pytest.raises(ValueError, match="expected 5 parts"):
        decode_nextauth_jwt("not.a.valid.token", TEST_SECRET)


def test_decode_jwt_unsupported_algorithm():
    header = _b64url_encode(json.dumps({"alg": "RSA-OAEP", "enc": "A256GCM"}).encode())
    token = f"{header}....".replace("....", "....")  # 5 parts
    # Construct a 5-part token with wrong algorithm
    parts = [header, "", _b64url_encode(b"iv"), _b64url_encode(b"ct"), _b64url_encode(b"tag")]
    with pytest.raises(ValueError, match="Unsupported JWE"):
        decode_nextauth_jwt(".".join(parts), TEST_SECRET)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db_conn():
    """Create a mock async database connection."""
    conn = AsyncMock()
    conn.row_factory = None
    return conn


@pytest.fixture
def client_with_mocks(mock_db_conn):
    """TestClient with overridden DB dependency and settings."""
    from chat_recall_api.deps import get_db
    from chat_recall_api.config import get_settings, Settings

    settings = Settings(
        database_url="postgresql://test:test@localhost/test",
        nextauth_secret=TEST_SECRET,
    )

    async def override_get_db():
        yield mock_db_conn

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = lambda: settings

    yield TestClient(app), mock_db_conn, settings

    app.dependency_overrides.clear()


# ── POST /auth/sync-user ─────────────────────────────────────────────────


def test_sync_user_creates_new(client_with_mocks):
    client, conn, settings = client_with_mocks

    # No existing user found
    mock_cur_empty = AsyncMock()
    mock_cur_empty.fetchone = AsyncMock(return_value=None)

    # New user created
    mock_cur_created = AsyncMock()
    mock_cur_created.fetchone = AsyncMock(return_value={
        "id": "new-uuid", "email": "test@example.com", "name": "Test",
        "github_id": "gh-123", "google_id": None, "avatar_url": None,
        "created_at": "2024-01-01", "updated_at": None,
    })

    call_count = 0
    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        if "INSERT INTO" in sql:
            return mock_cur_created
        return mock_cur_empty

    conn.execute = mock_execute

    response = client.post(
        "/auth/sync-user",
        json={"email": "test@example.com", "name": "Test", "github_id": "gh-123"},
        headers={"X-Internal-Key": TEST_SECRET},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "new-uuid"
    assert data["email"] == "test@example.com"


def test_sync_user_returns_existing(client_with_mocks):
    client, conn, settings = client_with_mocks

    existing_user = {
        "id": "existing-uuid", "email": "test@example.com", "name": "Existing",
        "github_id": "gh-123", "google_id": None, "avatar_url": None,
        "created_at": "2024-01-01", "updated_at": None,
    }
    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=existing_user)
    conn.execute = AsyncMock(return_value=mock_cur)

    response = client.post(
        "/auth/sync-user",
        json={"email": "test@example.com", "github_id": "gh-123"},
        headers={"X-Internal-Key": TEST_SECRET},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "existing-uuid"


def test_sync_user_forbidden_without_key(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.post(
        "/auth/sync-user",
        json={"email": "test@example.com"},
    )
    assert response.status_code == 403


def test_sync_user_forbidden_wrong_key(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.post(
        "/auth/sync-user",
        json={"email": "test@example.com"},
        headers={"X-Internal-Key": "wrong-key"},
    )
    assert response.status_code == 403


# ── GET /users/me ─────────────────────────────────────────────────────────


def test_get_me_success(client_with_mocks):
    client, conn, settings = client_with_mocks

    user_row = {
        "id": "user-uuid", "email": "me@example.com", "name": "Me",
        "github_id": "gh-1", "google_id": None, "avatar_url": None,
        "created_at": "2024-01-01", "updated_at": None,
    }
    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=user_row)
    conn.execute = AsyncMock(return_value=mock_cur)

    token = _make_jwe({"sub": "user-uuid", "email": "me@example.com"})

    response = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "user-uuid"
    assert data["email"] == "me@example.com"


def test_get_me_unauthorized_no_token(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.get("/users/me")
    assert response.status_code == 401


def test_get_me_unauthorized_invalid_token(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.get(
        "/users/me",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert response.status_code == 401


def test_get_me_user_not_found(client_with_mocks):
    client, conn, settings = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=mock_cur)

    token = _make_jwe({"sub": "nonexistent-uuid"})

    response = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


# ── PATCH /users/me ───────────────────────────────────────────────────────


def test_update_me_success(client_with_mocks):
    client, conn, settings = client_with_mocks

    updated_user = {
        "id": "user-uuid", "email": "me@example.com", "name": "New Name",
        "github_id": None, "google_id": None, "avatar_url": "https://avatar.com/new.png",
        "created_at": "2024-01-01", "updated_at": "2024-06-01",
    }

    call_count = 0
    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if "UPDATE" in sql:
            return m
        m.fetchone = AsyncMock(return_value=updated_user)
        return m

    conn.execute = mock_execute

    token = _make_jwe({"sub": "user-uuid"})

    response = client.patch(
        "/users/me",
        json={"name": "New Name", "avatar_url": "https://avatar.com/new.png"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Name"


def test_update_me_no_fields(client_with_mocks):
    client, conn, settings = client_with_mocks

    token = _make_jwe({"sub": "user-uuid"})

    response = client.patch(
        "/users/me",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assert "No fields" in response.json()["detail"]


def test_update_me_unauthorized(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.patch(
        "/users/me",
        json={"name": "Hacker"},
    )

    assert response.status_code == 401


# ── DELETE /account ──────────────────────────────────────────────────────


def test_delete_account_success(client_with_mocks):
    client, conn, settings = client_with_mocks

    user_row = {"id": "user-uuid"}

    call_count = 0
    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if call_count == 1:
            # SELECT id FROM users
            m.fetchone = AsyncMock(return_value=user_row)
        else:
            # DELETE statements
            m.rowcount = 0
        return m

    conn.execute = mock_execute

    token = _make_jwe({"sub": "user-uuid"})

    response = client.delete(
        "/account",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["deleted"] is True
    assert "counts" in data


def test_delete_account_user_not_found(client_with_mocks):
    client, conn, settings = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=mock_cur)

    token = _make_jwe({"sub": "nonexistent-uuid"})

    response = client.delete(
        "/account",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


def test_delete_account_unauthorized(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.delete("/account")

    assert response.status_code == 401


# ── GET /export ──────────────────────────────────────────────────────────


def test_export_success(client_with_mocks):
    client, conn, settings = client_with_mocks

    user_row = {
        "id": "user-uuid", "email": "me@example.com", "name": "Me",
        "github_id": "gh-1", "google_id": None, "avatar_url": None,
        "created_at": "2024-01-01", "updated_at": None,
    }
    conversation_row = {
        "id": "conv-1", "user_id": "user-uuid", "title": "Hello",
        "created_at": "2024-01-02", "updated_at": None,
    }
    message_row = {
        "id": "msg-1", "conversation_id": "conv-1", "role": "user",
        "content_text": "Hi there", "raw_content": '{"parts": ["Hi there"]}',
        "created_at": "2024-01-02",
    }
    thread_row = {
        "id": "thread-1", "user_id": "user-uuid", "name": "Thread 1",
        "created_at": "2024-01-03",
    }
    thread_conv_row = {
        "thread_id": "thread-1", "conversation_id": "conv-1",
    }
    upload_row = {
        "id": "upload-1", "user_id": "user-uuid", "filename": "export.zip",
        "created_at": "2024-01-04",
    }
    sub_row = {
        "id": "sub-1", "user_id": "user-uuid", "status": "active",
        "stripe_subscription_id": "sub_123",
    }

    call_count = 0

    async def mock_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        m = AsyncMock()
        if call_count == 1:
            # SELECT * FROM users
            m.fetchone = AsyncMock(return_value=user_row)
        elif call_count == 2:
            # SELECT * FROM conversations
            m.fetchall = AsyncMock(return_value=[conversation_row])
        elif call_count == 3:
            # SELECT m.* FROM messages
            m.fetchall = AsyncMock(return_value=[message_row])
        elif call_count == 4:
            # SELECT * FROM threads
            m.fetchall = AsyncMock(return_value=[thread_row])
        elif call_count == 5:
            # SELECT tc.* FROM thread_conversations
            m.fetchall = AsyncMock(return_value=[thread_conv_row])
        elif call_count == 6:
            # SELECT * FROM uploads
            m.fetchall = AsyncMock(return_value=[upload_row])
        elif call_count == 7:
            # SELECT * FROM subscriptions
            m.fetchone = AsyncMock(return_value=sub_row)
        return m

    conn.execute = mock_execute

    token = _make_jwe({"sub": "user-uuid", "email": "me@example.com"})

    response = client.get(
        "/export",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert "chat-recall-export.json" in response.headers["content-disposition"]

    data = response.json()
    assert data["user"]["id"] == "user-uuid"
    assert len(data["conversations"]) == 1
    assert data["conversations"][0]["id"] == "conv-1"
    assert len(data["messages"]) == 1
    assert data["messages"][0]["content_text"] == "Hi there"
    assert len(data["threads"]) == 1
    assert len(data["thread_conversations"]) == 1
    assert len(data["uploads"]) == 1
    assert data["subscription"]["status"] == "active"
    assert "exported_at" in data


def test_export_user_not_found(client_with_mocks):
    client, conn, settings = client_with_mocks

    mock_cur = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=mock_cur)

    token = _make_jwe({"sub": "nonexistent-uuid"})

    response = client.get(
        "/export",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


def test_export_unauthorized(client_with_mocks):
    client, conn, settings = client_with_mocks

    response = client.get("/export")

    assert response.status_code == 401
