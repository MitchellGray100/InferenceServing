"""Project API key generation and verification helpers.

Raw project API keys are credentials, so only the caller sees them once at
creation time. The database stores a short routing prefix plus a keyed HMAC of
the full key; this lets the inference service find likely candidates without
storing a reusable secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from flask import current_app

from app.utils.errors import ApiError


KEY_PREFIX = "mt_live"
VISIBLE_TOKEN_BYTES = 9
SECRET_TOKEN_BYTES = 24
HASH_ALGORITHM = "sha256"


def generate_api_key() -> tuple[str, str]:
    """Generate a raw API key and return it with its visible DB prefix."""
    visible = _token(VISIBLE_TOKEN_BYTES)
    secret = _token(SECRET_TOKEN_BYTES)
    raw_key = f"{KEY_PREFIX}_{visible}_{secret}"
    return raw_key, derive_key_prefix(raw_key)


def derive_key_prefix(raw_key: str) -> str:
    """Return the non-secret lookup prefix embedded in a raw API key."""
    parts = raw_key.split("_", maxsplit=3)

    if len(parts) != 4 or f"{parts[0]}_{parts[1]}" != KEY_PREFIX:
        raise invalid_api_key_error()

    return f"{KEY_PREFIX}_{parts[2]}"


def hash_api_key(raw_key: str, secret: str | None = None) -> str:
    """Hash a raw API key using a server-side secret."""
    hash_secret = (secret or current_app.config["API_KEY_HASH_SECRET"]).encode()
    digest = hmac.new(hash_secret, raw_key.encode(), hashlib.sha256).hexdigest()
    return f"{HASH_ALGORITHM}:{digest}"


def verify_api_key(raw_key: str, stored_hash: str, secret: str | None = None) -> bool:
    """Constant-time comparison of a raw API key against a stored HMAC."""
    expected_hash = hash_api_key(raw_key, secret)
    return hmac.compare_digest(expected_hash, stored_hash)


def invalid_api_key_error() -> ApiError:
    """Build the standard invalid project API key error."""
    return ApiError(
        type="unauthorized",
        message="Missing or invalid project API key.",
        status_code=401,
    )


def _token(num_bytes: int) -> str:
    """Return URL-safe random text without padding."""
    return base64.urlsafe_b64encode(secrets.token_bytes(num_bytes)).decode().rstrip("=")
