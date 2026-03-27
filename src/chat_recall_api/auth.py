"""JWT/JWE validation for NextAuth tokens.

NextAuth v5 encrypts session JWTs using:
- Algorithm: dir (direct key agreement)
- Encryption: A256GCM
- Key: HKDF-SHA256 derived from NEXTAUTH_SECRET

This module decrypts those tokens to extract user claims.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Depends, HTTPException, Request, status

from chat_recall_api.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _b64url_decode(data: str) -> bytes:
    """Base64url decode with padding."""
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data)


def _derive_encryption_key(secret: str) -> bytes:
    """Derive the AES-256 encryption key from NEXTAUTH_SECRET using HKDF.

    Matches NextAuth's key derivation:
        hkdf("sha256", secret, "", "NextAuth.js Generated Encryption Key", 32)
    """
    hkdf = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=b"",
        info=b"NextAuth.js Generated Encryption Key",
    )
    return hkdf.derive(secret.encode("utf-8"))


def decode_nextauth_jwt(token: str, secret: str) -> dict[str, Any]:
    """Decrypt a NextAuth v5 JWE token and return its claims.

    Args:
        token: The JWE compact serialization string (5 dot-separated parts).
        secret: The NEXTAUTH_SECRET used by the web app.

    Returns:
        Dict of JWT claims (sub, name, email, picture, iat, exp, jti).

    Raises:
        ValueError: If the token format is invalid or decryption fails.
    """
    parts = token.split(".")
    if len(parts) != 5:
        raise ValueError("Invalid JWE token: expected 5 parts")

    header_b64, _enc_key_b64, iv_b64, ciphertext_b64, tag_b64 = parts

    header = json.loads(_b64url_decode(header_b64))
    if header.get("alg") != "dir" or header.get("enc") != "A256GCM":
        raise ValueError(f"Unsupported JWE: alg={header.get('alg')}, enc={header.get('enc')}")

    iv = _b64url_decode(iv_b64)
    ciphertext = _b64url_decode(ciphertext_b64)
    tag = _b64url_decode(tag_b64)
    aad = header_b64.encode("ascii")

    key = _derive_encryption_key(secret)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(iv, ciphertext + tag, aad)

    return json.loads(plaintext)


async def get_current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """FastAPI dependency: extract and validate the NextAuth session token.

    Expects: Authorization: Bearer <nextauth-jwe-token>

    Returns:
        Dict with user claims from the decrypted token.

    Raises:
        HTTPException 401 if token is missing or invalid.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    token = auth_header[7:]  # Strip "Bearer "
    if not settings.nextauth_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="NEXTAUTH_SECRET not configured",
        )

    try:
        claims = decode_nextauth_jwt(token, settings.nextauth_secret)
    except Exception as e:
        logger.warning("JWT decode failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    if not claims.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject claim",
        )

    exp = claims.get("exp")
    if exp and time.time() > exp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )

    return claims


def verify_internal_key(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """FastAPI dependency: verify X-Internal-Key header for server-to-server calls.

    Used by POST /auth/sync-user (called from NextAuth callback).
    """
    key = request.headers.get("X-Internal-Key", "")
    if not settings.nextauth_secret or key != settings.nextauth_secret:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid internal key",
        )
