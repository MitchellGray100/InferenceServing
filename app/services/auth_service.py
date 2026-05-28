"""Authentication business logic.

This service normalizes emails, verifies password hashes, updates login
metadata, and issues stateless user access tokens.
"""

from __future__ import annotations

from typing import Any

from app.db.pool import transaction
from app.db.sql import load_queries
from app.security.passwords import verify_password
from app.security.tokens import create_access_token
from app.services.user_service import serialize_user
from app.utils.errors import ApiError, ValidationError
from app.utils.validation import normalize_email, validate_string


# Auth uses user queries because credentials live on the users table. Service
# functions still return only public user fields through `serialize_user`.
queries = load_queries()


def login(email: Any, password: Any) -> dict[str, Any]:
    """Authenticate a user and return a bearer token plus user info.

    Validation errors are intentionally collapsed into the same
    `invalid_credentials` response as password mismatch so login cannot be used
    to enumerate registered emails or infer password policy details.
    """
    try:
        normalized_email = normalize_email(email)
        plaintext_password = validate_string(password, "password")
    except ValidationError as exc:
        raise invalid_credentials_error() from exc

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("get_user_auth_by_email"),
                {"email": normalized_email},
            )
            auth_row = cur.fetchone()

            if auth_row is None or not verify_password(
                plaintext_password,
                auth_row["hashed_password"],
            ):
                raise invalid_credentials_error()

            cur.execute(
                queries.get("update_user_last_login"),
                {"user_id": auth_row["user_id"]},
            )
            user_row = cur.fetchone()

    return {
        "access_token": create_access_token(str(auth_row["user_id"])),
        "token_type": "bearer",
        "user": serialize_user(user_row),
    }


def logout() -> dict[str, bool]:
    """Return a consistent logout response for stateless bearer tokens.

    The MVP does not store server-side token sessions, so logout is a client
    action: discard the bearer token. A token denylist can be added later if
    the product needs immediate revocation before token expiry.
    """
    return {"logged_out": True}


def invalid_credentials_error() -> ApiError:
    """Build the standard login failure error without account enumeration."""
    return ApiError(
        type="invalid_credentials",
        message="Invalid email or password.",
        status_code=401,
    )
