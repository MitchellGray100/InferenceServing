"""User account business logic.

This service validates user input, hashes passwords, and persists user account
records through named SQL queries.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.pool import transaction
from app.db.sql import load_queries
from app.security.passwords import hash_password
from app.services import project_service
from app.utils.errors import ApiError
from app.utils.time import to_iso8601
from app.utils.validation import normalize_email, validate_password, validate_uuid


# User queries are kept in SQL files so password hashing and response shaping
# stay in Python while persistence details remain reviewable as SQL.
queries = load_queries()
logger = logging.getLogger(__name__)


def create_user(email: Any, password: Any) -> dict[str, Any]:
    """Create a user account and return public user fields.

    Password hashing happens before the insert so raw passwords never cross the
    database boundary.
    """
    # Normalize/validate first so the database only sees canonical email text
    # and never receives an invalid or short plaintext password.
    normalized_email = normalize_email(email)
    plaintext_password = validate_password(password)
    hashed_password = hash_password(plaintext_password)

    try:
        with transaction() as conn:
            with conn.cursor() as cur:
                # Store only the password hash. Argon2 embeds its own salt and
                # parameters inside this string.
                cur.execute(
                    queries.get("create_user"),
                    {
                        "email": normalized_email,
                        "hashed_password": hashed_password,
                    },
                )
                row = cur.fetchone()
    except Exception as exc:
        if _is_unique_violation(exc):
            logger.info("User creation rejected because email already exists.")
            raise ApiError(
                type="email_already_exists",
                message="A user with this email already exists.",
                status_code=409,
            ) from exc
        raise

    logger.info("Created user user_id=%s.", row["user_id"])
    return serialize_user(row)


def get_user(user_id: Any) -> dict[str, Any]:
    """Return a public user record by user_id."""
    # Canonical UUID strings make query parameters consistent even if callers
    # pass uppercase or otherwise equivalent UUID text.
    canonical_user_id = validate_uuid(user_id, "userID")

    with transaction() as conn:
        with conn.cursor() as cur:
            # The SQL query selects public account fields plus last_login_at; it
            # does not expose hashed_password to this read path.
            cur.execute(
                queries.get("get_user_by_id"),
                {"user_id": canonical_user_id},
            )
            row = cur.fetchone()

    if row is None:
        logger.info("User lookup missed user_id=%s.", canonical_user_id)
        raise ApiError(
            type="user_not_found",
            message="User not found.",
            status_code=404,
        )

    logger.debug("Fetched user user_id=%s.", canonical_user_id)
    return serialize_user(row)


def delete_user(user_id: Any) -> dict[str, bool]:
    """Delete the authenticated user account.

    Projects where this user is the only owner are deleted first so account
    deletion cannot leave ownerless projects or orphaned Kubernetes resources.
    """
    # The current `/me` route supplies this ID from the bearer token, so callers
    # cannot delete arbitrary user IDs through the public API.
    canonical_user_id = validate_uuid(user_id, "userID")

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("get_user_by_id"),
                {"user_id": canonical_user_id},
            )
            existing_user = cur.fetchone()

    if existing_user is None:
        logger.info("User delete missed user_id=%s.", canonical_user_id)
        raise ApiError(
            type="user_not_found",
            message="User not found.",
            status_code=404,
        )

    deleted_projects = project_service.delete_sole_owner_projects_for_user(
        canonical_user_id
    )

    with transaction() as conn:
        with conn.cursor() as cur:
            # The delete query returns a row only when a user existed, letting
            # the service distinguish a real deletion from a stale token.
            cur.execute(
                queries.get("delete_user"),
                {"user_id": canonical_user_id},
            )
            row = cur.fetchone()

    if row is None:
        logger.info("User delete missed user_id=%s.", canonical_user_id)
        raise ApiError(
            type="user_not_found",
            message="User not found.",
            status_code=404,
        )

    logger.info(
        "Deleted user user_id=%s sole_owner_projects_deleted=%s.",
        canonical_user_id,
        len(deleted_projects),
    )
    return {"deleted": True}


def serialize_user(row: Any) -> dict[str, Any]:
    """Serialize a database user row into API response shape."""
    # Keep all user response shaping in one place so route functions cannot
    # accidentally return hashed_password or other internal fields.
    return {
        "userID": str(row["user_id"]),
        "email": row["email"],
        "created_at": to_iso8601(row["created_at"]),
        "last_login_at": (
            to_iso8601(row["last_login_at"]) if row["last_login_at"] else None
        ),
    }


def _is_unique_violation(exc: Exception) -> bool:
    """Detect psycopg unique violations without importing psycopg globally."""
    return exc.__class__.__name__ == "UniqueViolation"
