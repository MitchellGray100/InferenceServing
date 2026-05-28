"""User account business logic.

This service validates user input, hashes passwords, and persists user account
records through named SQL queries.
"""

from __future__ import annotations

from typing import Any

from app.db.pool import transaction
from app.db.sql import load_queries
from app.security.passwords import hash_password
from app.utils.errors import ApiError
from app.utils.time import to_iso8601
from app.utils.validation import normalize_email, validate_password, validate_uuid


queries = load_queries()


def create_user(email: Any, password: Any) -> dict[str, Any]:
    """Create a user account and return public user fields."""
    normalized_email = normalize_email(email)
    plaintext_password = validate_password(password)
    hashed_password = hash_password(plaintext_password)

    try:
        with transaction() as conn:
            with conn.cursor() as cur:
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
            raise ApiError(
                type="email_already_exists",
                message="A user with this email already exists.",
                status_code=409,
            ) from exc
        raise

    return serialize_user(row)


def get_user(user_id: Any) -> dict[str, Any]:
    """Return a public user record by user_id."""
    canonical_user_id = validate_uuid(user_id, "userID")

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("get_user_by_id"),
                {"user_id": canonical_user_id},
            )
            row = cur.fetchone()

    if row is None:
        raise ApiError(
            type="user_not_found",
            message="User not found.",
            status_code=404,
        )

    return serialize_user(row)


def delete_user(user_id: Any) -> dict[str, bool]:
    """Delete the authenticated user account."""
    canonical_user_id = validate_uuid(user_id, "userID")

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("delete_user"),
                {"user_id": canonical_user_id},
            )
            row = cur.fetchone()

    if row is None:
        raise ApiError(
            type="user_not_found",
            message="User not found.",
            status_code=404,
        )

    return {"deleted": True}


def serialize_user(row: Any) -> dict[str, Any]:
    """Serialize a database user row into API response shape."""
    return {
        "userID": str(row["user_id"]),
        "email": row["email"],
        "created_at": to_iso8601(row["created_at"]),
        "last_login_at": (
            to_iso8601(row["last_login_at"]) if row["last_login_at"] else None
        ),
    }


def _is_unique_violation(exc: Exception) -> bool:
    return exc.__class__.__name__ == "UniqueViolation"
