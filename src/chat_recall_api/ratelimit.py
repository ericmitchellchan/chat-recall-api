"""In-memory sliding-window rate limiter.

Provides per-user and per-IP rate limiting as FastAPI dependencies.
Uses an in-memory store — suitable for single-instance deployments.
Swap to Redis-backed store if horizontal scaling is needed.
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request, status


class _SlidingWindow:
    """Thread-safe sliding window counter."""

    def __init__(self) -> None:
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        """Check if a request is allowed.

        Returns (allowed, remaining) where remaining is how many requests
        are left in the current window.
        """
        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            timestamps = self._windows[key]
            # Prune expired entries
            self._windows[key] = [t for t in timestamps if t > cutoff]
            timestamps = self._windows[key]

            if len(timestamps) >= max_requests:
                return False, 0

            timestamps.append(now)
            remaining = max_requests - len(timestamps)
            return True, remaining

    def reset(self) -> None:
        """Clear all windows (useful for testing)."""
        with self._lock:
            self._windows.clear()


# Global instance
_limiter = _SlidingWindow()


def get_limiter() -> _SlidingWindow:
    """Return the global rate limiter instance."""
    return _limiter


def _get_client_ip(request: Request) -> str:
    """Extract client IP, respecting X-Forwarded-For behind ALB."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _get_rate_key(request: Request) -> str:
    """Build rate limit key: user_id if authenticated, otherwise IP."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and len(auth) > 20:
        # Use a hash of the token prefix as the key — avoids decrypting
        # the full JWE just for rate limiting. The actual auth check
        # happens in the endpoint dependency.
        return f"user:{hash(auth[:50])}"
    return f"ip:{_get_client_ip(request)}"


def rate_limit(max_requests: int, window_seconds: int):
    """Create a FastAPI dependency that enforces a rate limit.

    Usage:
        @router.post("/upload", dependencies=[Depends(rate_limit(5, 3600))])
        async def upload_file(...):

    Args:
        max_requests: Maximum requests allowed in the window.
        window_seconds: Window duration in seconds.
    """

    async def _check(request: Request) -> None:
        key = _get_rate_key(request)
        scope = request.url.path
        full_key = f"{scope}:{key}"

        allowed, remaining = _limiter.is_allowed(full_key, max_requests, window_seconds)

        # Set rate limit headers
        request.state.ratelimit_remaining = remaining
        request.state.ratelimit_limit = max_requests

        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please try again later.",
                headers={"Retry-After": str(window_seconds)},
            )

    return _check
