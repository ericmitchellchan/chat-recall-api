"""User schemas for request validation and response serialization."""

from __future__ import annotations

from pydantic import BaseModel


class UserSync(BaseModel):
    """Request body for POST /auth/sync-user (upsert from OAuth callback)."""

    email: str
    name: str | None = None
    github_id: str | None = None
    google_id: str | None = None
    avatar_url: str | None = None


class UserResponse(BaseModel):
    """User profile response."""

    id: str
    email: str
    name: str | None = None
    github_id: str | None = None
    google_id: str | None = None
    avatar_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class UserUpdate(BaseModel):
    """Request body for PATCH /users/me (update profile)."""

    name: str | None = None
    avatar_url: str | None = None
