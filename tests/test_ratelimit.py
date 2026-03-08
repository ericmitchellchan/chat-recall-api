"""Tests for the rate limiting module."""

import asyncio

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from chat_recall_api.ratelimit import _SlidingWindow, rate_limit, get_limiter


# ── Unit tests for SlidingWindow ─────────────────────────────────────────


def test_sliding_window_allows_under_limit():
    sw = _SlidingWindow()
    allowed, remaining = sw.is_allowed("key1", 5, 60)
    assert allowed is True
    assert remaining == 4


def test_sliding_window_blocks_at_limit():
    sw = _SlidingWindow()
    for _ in range(5):
        sw.is_allowed("key2", 5, 60)

    allowed, remaining = sw.is_allowed("key2", 5, 60)
    assert allowed is False
    assert remaining == 0


def test_sliding_window_separate_keys():
    sw = _SlidingWindow()
    for _ in range(5):
        sw.is_allowed("keyA", 5, 60)

    # keyB should still be allowed
    allowed, remaining = sw.is_allowed("keyB", 5, 60)
    assert allowed is True
    assert remaining == 4


def test_sliding_window_remaining_decrements():
    sw = _SlidingWindow()
    _, r1 = sw.is_allowed("key3", 5, 60)
    _, r2 = sw.is_allowed("key3", 5, 60)
    _, r3 = sw.is_allowed("key3", 5, 60)
    assert r1 == 4
    assert r2 == 3
    assert r3 == 2


def test_sliding_window_reset():
    sw = _SlidingWindow()
    for _ in range(5):
        sw.is_allowed("key4", 5, 60)

    sw.reset()

    allowed, remaining = sw.is_allowed("key4", 5, 60)
    assert allowed is True
    assert remaining == 4


# ── Integration tests with FastAPI ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_limiter():
    """Reset the global limiter before each test."""
    get_limiter().reset()
    yield
    get_limiter().reset()


@pytest.fixture
def rate_limited_app():
    """Create a minimal FastAPI app with rate-limited endpoints."""
    app = FastAPI()

    @app.get("/limited", dependencies=[Depends(rate_limit(3, 60))])
    async def limited_endpoint():
        return {"ok": True}

    @app.get("/unlimited")
    async def unlimited_endpoint():
        return {"ok": True}

    return TestClient(app)


def test_rate_limit_allows_requests(rate_limited_app):
    for _ in range(3):
        resp = rate_limited_app.get("/limited")
        assert resp.status_code == 200


def test_rate_limit_blocks_excess(rate_limited_app):
    for _ in range(3):
        rate_limited_app.get("/limited")

    resp = rate_limited_app.get("/limited")
    assert resp.status_code == 429
    assert "rate limit" in resp.json()["detail"].lower()
    assert "Retry-After" in resp.headers


def test_rate_limit_different_paths():
    """Different paths have separate rate limits."""
    app = FastAPI()

    @app.get("/a", dependencies=[Depends(rate_limit(2, 60))])
    async def endpoint_a():
        return {"ok": True}

    @app.get("/b", dependencies=[Depends(rate_limit(2, 60))])
    async def endpoint_b():
        return {"ok": True}

    client = TestClient(app)

    # Use up limit on /a
    for _ in range(2):
        client.get("/a")

    # /b should still work
    resp = client.get("/b")
    assert resp.status_code == 200


def test_rate_limit_per_user():
    """Different auth tokens get separate rate limits."""
    app = FastAPI()

    @app.get("/user-limited", dependencies=[Depends(rate_limit(2, 60))])
    async def user_endpoint():
        return {"ok": True}

    client = TestClient(app)

    # User A hits the limit
    for _ in range(2):
        client.get("/user-limited", headers={"Authorization": "Bearer tokenAAAAAAAAAAAAAAAAAAAAAAAAAAA"})

    blocked = client.get("/user-limited", headers={"Authorization": "Bearer tokenAAAAAAAAAAAAAAAAAAAAAAAAAAA"})
    assert blocked.status_code == 429

    # User B should still be allowed
    resp = client.get("/user-limited", headers={"Authorization": "Bearer tokenBBBBBBBBBBBBBBBBBBBBBBBBBBB"})
    assert resp.status_code == 200


def test_unlimited_endpoint_not_affected(rate_limited_app):
    """Endpoints without rate_limit should not be affected."""
    for _ in range(10):
        resp = rate_limited_app.get("/unlimited")
        assert resp.status_code == 200
