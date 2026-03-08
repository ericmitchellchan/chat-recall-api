"""Tests for the upload endpoint and ChatGPT importer."""

import io
import json
import zipfile
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from chat_recall_api.config import Settings, get_settings
from chat_recall_api.content import extract_text
from chat_recall_api.deps import get_db
from chat_recall_api.importer import _parse_conversation, _trace_canonical_path
from chat_recall_api.main import app

from tests.test_users import TEST_SECRET, _make_jwe


# ── Fixtures ──────────────────────────────────────────────────────────────


SAMPLE_CONVERSATION = {
    "id": "conv-001",
    "title": "Test Conversation",
    "create_time": 1704067200.0,
    "update_time": 1704153600.0,
    "default_model_slug": "gpt-4o",
    "current_node": "node-3",
    "mapping": {
        "node-1": {
            "id": "node-1",
            "parent": None,
            "children": ["node-2"],
            "message": {
                "id": "msg-1",
                "author": {"role": "system"},
                "content": {"content_type": "text", "parts": ["You are a helpful assistant."]},
                "create_time": 1704067200.0,
                "metadata": {},
            },
        },
        "node-2": {
            "id": "node-2",
            "parent": "node-1",
            "children": ["node-3"],
            "message": {
                "id": "msg-2",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["Hello, how are you?"]},
                "create_time": 1704067201.0,
                "metadata": {},
            },
        },
        "node-3": {
            "id": "node-3",
            "parent": "node-2",
            "children": [],
            "message": {
                "id": "msg-3",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["I'm doing well, thank you!"]},
                "create_time": 1704067202.0,
                "metadata": {"model_slug": "gpt-4o", "finish_details": {"type": "end_turn"}},
            },
        },
    },
}


@pytest.fixture
def mock_db_conn():
    conn = AsyncMock()
    conn.row_factory = None
    return conn


@pytest.fixture
def client_with_mocks(mock_db_conn):
    settings = Settings(
        database_url="postgresql://test:test@localhost/test",
        nextauth_secret=TEST_SECRET,
    )

    async def override_get_db():
        yield mock_db_conn

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = lambda: settings

    yield TestClient(app), mock_db_conn

    app.dependency_overrides.clear()


def _auth_header(user_id="user-uuid"):
    token = _make_jwe({"sub": user_id, "email": "test@example.com"})
    return {"Authorization": f"Bearer {token}"}


def _make_zip(conversations: list[dict], inner_name: str = "conversations.json") -> bytes:
    """Create a ZIP file containing conversations.json."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_name, json.dumps(conversations))
    return buf.getvalue()


# ── Content extraction ────────────────────────────────────────────────────


def test_extract_text_plain():
    content = {"content_type": "text", "parts": ["Hello", "World"]}
    assert extract_text(content) == "Hello\nWorld"


def test_extract_text_code():
    content = {"content_type": "code", "text": "print('hi')", "language": "python"}
    assert extract_text(content) == "```python\nprint('hi')\n```"


def test_extract_text_multimodal():
    content = {
        "content_type": "multimodal_text",
        "parts": [
            "Some text",
            {"content_type": "image_asset_pointer"},
            {"text": "More text"},
        ],
    }
    assert extract_text(content) == "Some text\nMore text"


def test_extract_text_none():
    assert extract_text(None) == ""


def test_extract_text_empty():
    assert extract_text({}) == ""


# ── Conversation parsing ─────────────────────────────────────────────────


def test_parse_conversation_basic():
    messages, has_branches = _parse_conversation(SAMPLE_CONVERSATION, "conv-001")

    assert len(messages) == 3
    assert has_branches is False

    roles = {m["role"] for m in messages}
    assert roles == {"system", "user", "assistant"}

    # All should be canonical (linear path)
    assert all(m["is_canonical"] for m in messages)


def test_parse_conversation_with_branches():
    conv = {
        "id": "conv-branch",
        "current_node": "node-3a",
        "mapping": {
            "node-1": {
                "id": "node-1",
                "parent": None,
                "children": ["node-2"],
                "message": {
                    "id": "msg-1",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Hi"]},
                    "create_time": 1.0,
                    "metadata": {},
                },
            },
            "node-2": {
                "id": "node-2",
                "parent": "node-1",
                "children": ["node-3a", "node-3b"],  # Branch!
                "message": {
                    "id": "msg-2",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["Response"]},
                    "create_time": 2.0,
                    "metadata": {},
                },
            },
            "node-3a": {
                "id": "node-3a",
                "parent": "node-2",
                "children": [],
                "message": {
                    "id": "msg-3a",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Follow up A"]},
                    "create_time": 3.0,
                    "metadata": {},
                },
            },
            "node-3b": {
                "id": "node-3b",
                "parent": "node-2",
                "children": [],
                "message": {
                    "id": "msg-3b",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Follow up B"]},
                    "create_time": 3.5,
                    "metadata": {},
                },
            },
        },
    }
    messages, has_branches = _parse_conversation(conv, "conv-branch")

    assert len(messages) == 4
    assert has_branches is True

    # node-3a is canonical (current_node), node-3b is not
    canonical_msgs = [m for m in messages if m["is_canonical"]]
    non_canonical = [m for m in messages if not m["is_canonical"]]
    assert len(canonical_msgs) == 3  # node-1, node-2, node-3a
    assert len(non_canonical) == 1  # node-3b
    assert non_canonical[0]["id"] == "msg-3b"


def test_parse_conversation_empty_mapping():
    conv = {"id": "empty", "mapping": {}}
    messages, has_branches = _parse_conversation(conv, "empty")
    assert messages == []
    assert has_branches is False


def test_trace_canonical_path():
    mapping = {
        "a": {"parent": None},
        "b": {"parent": "a"},
        "c": {"parent": "b"},
    }
    canonical = _trace_canonical_path(mapping, "c")
    assert canonical == {"a", "b", "c"}


def test_trace_canonical_path_none():
    assert _trace_canonical_path({}, None) == set()


# ── Upload endpoint ──────────────────────────────────────────────────────


@patch("chat_recall_api.routers.upload.import_chatgpt_data")
def test_upload_json(mock_import, client_with_mocks):
    client, conn = client_with_mocks

    mock_import.return_value = {
        "source_id": 1,
        "conversations_imported": 1,
        "conversations_updated": 0,
        "conversations_skipped": 0,
        "messages_imported": 3,
        "skip_reasons": {},
        "errors": [],
    }
    conn.execute = AsyncMock()

    conversations = [SAMPLE_CONVERSATION]
    file_content = json.dumps(conversations).encode()

    response = client.post(
        "/upload",
        files={"file": ("conversations.json", io.BytesIO(file_content), "application/json")},
        headers=_auth_header(),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["conversations_imported"] == 1
    assert data["messages_imported"] == 3
    assert "upload_id" in data
    mock_import.assert_called_once()


@patch("chat_recall_api.routers.upload.import_chatgpt_data")
def test_upload_zip(mock_import, client_with_mocks):
    client, conn = client_with_mocks

    mock_import.return_value = {
        "source_id": 1,
        "conversations_imported": 2,
        "conversations_updated": 0,
        "conversations_skipped": 0,
        "messages_imported": 10,
        "skip_reasons": {},
        "errors": [],
    }
    conn.execute = AsyncMock()

    zip_bytes = _make_zip([SAMPLE_CONVERSATION, SAMPLE_CONVERSATION])

    response = client.post(
        "/upload",
        files={"file": ("chatgpt-export.zip", io.BytesIO(zip_bytes), "application/zip")},
        headers=_auth_header(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_upload_invalid_file_type(client_with_mocks):
    client, conn = client_with_mocks

    response = client.post(
        "/upload",
        files={"file": ("data.csv", io.BytesIO(b"a,b,c"), "text/csv")},
        headers=_auth_header(),
    )

    assert response.status_code == 400
    assert "json" in response.json()["detail"].lower() or "zip" in response.json()["detail"].lower()


def test_upload_empty_file(client_with_mocks):
    client, conn = client_with_mocks

    response = client.post(
        "/upload",
        files={"file": ("empty.json", io.BytesIO(b""), "application/json")},
        headers=_auth_header(),
    )

    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_upload_invalid_json(client_with_mocks):
    client, conn = client_with_mocks

    response = client.post(
        "/upload",
        files={"file": ("bad.json", io.BytesIO(b"not json{{{"), "application/json")},
        headers=_auth_header(),
    )

    assert response.status_code == 400
    assert "json" in response.json()["detail"].lower()


def test_upload_empty_conversations(client_with_mocks):
    client, conn = client_with_mocks

    response = client.post(
        "/upload",
        files={"file": ("empty.json", io.BytesIO(b"[]"), "application/json")},
        headers=_auth_header(),
    )

    assert response.status_code == 400
    assert "no conversations" in response.json()["detail"].lower()


def test_upload_zip_no_conversations_json(client_with_mocks):
    client, conn = client_with_mocks

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no conversations here")
    zip_bytes = buf.getvalue()

    response = client.post(
        "/upload",
        files={"file": ("export.zip", io.BytesIO(zip_bytes), "application/zip")},
        headers=_auth_header(),
    )

    assert response.status_code == 400
    assert "conversations.json" in response.json()["detail"]


def test_upload_unauthorized(client_with_mocks):
    client, conn = client_with_mocks

    response = client.post(
        "/upload",
        files={"file": ("test.json", io.BytesIO(b"[]"), "application/json")},
    )

    assert response.status_code == 401


def test_upload_single_conversation_object(client_with_mocks):
    """A single conversation (not wrapped in array) should work."""
    client, conn = client_with_mocks

    conn.execute = AsyncMock()

    with patch("chat_recall_api.routers.upload.import_chatgpt_data") as mock_import:
        mock_import.return_value = {
            "source_id": 1,
            "conversations_imported": 1,
            "conversations_updated": 0,
            "conversations_skipped": 0,
            "messages_imported": 3,
            "skip_reasons": {},
            "errors": [],
        }

        file_content = json.dumps(SAMPLE_CONVERSATION).encode()
        response = client.post(
            "/upload",
            files={"file": ("single.json", io.BytesIO(file_content), "application/json")},
            headers=_auth_header(),
        )

    assert response.status_code == 200
    # import should be called with a list of 1 conversation
    call_args = mock_import.call_args[0]
    assert isinstance(call_args[2], list)
    assert len(call_args[2]) == 1
