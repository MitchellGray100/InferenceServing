"""Password hashing and verification.

This module will wrap Argon2id or bcrypt so the rest of the app never handles
plaintext passwords beyond request validation.
"""

from __future__ import annotations

import logging

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError


_hasher = PasswordHasher()
logger = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage."""
    return _hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    """Return whether a plaintext password matches a stored password hash."""
    try:
        return _hasher.verify(hashed_password, password)
    except (VerifyMismatchError, VerificationError):
        logger.debug("Password verification failed.")
        return False
