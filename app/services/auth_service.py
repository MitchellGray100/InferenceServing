"""Authentication business logic.

This service normalizes emails, verifies password hashes, updates login
metadata, and issues stateless user access tokens.
"""

from __future__ import annotations

import logging
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
logger = logging.getLogger(__name__)


def login(email: Any, password: Any) -> dict[str, Any]:
    """Authenticate a user and return a bearer token plus user info.

    Validation errors are intentionally collapsed into the same
    `invalid_credentials` response as password mismatch so login cannot be used
    to enumerate registered emails or infer password policy details.
    """
    try:
        # Email normalization is safe to expose, but any validation failure is
        # converted to `invalid_credentials` below for a uniform login response.
        normalized_email = normalize_email(email)
        plaintext_password = validate_string(password, "password")
    except ValidationError as exc:
        logger.info("Login rejected during credential validation.")
        raise invalid_credentials_error() from exc

    with transaction() as conn:
        with conn.cursor() as cur:
            # This auth-specific query is allowed to load the password hash; the
            # normal user serialization path never returns it.
            cur.execute(
                queries.get("get_user_auth_by_email"),
                {"email": normalized_email},
            )
            auth_row = cur.fetchone()

            # Missing users and password mismatches intentionally share the
            # same response so login cannot be used for email enumeration.
            if auth_row is None or not verify_password(
                plaintext_password,
                auth_row["hashed_password"],
            ):
                logger.info("Login rejected for email=%s.", normalized_email)
                raise invalid_credentials_error()

            # Record successful login time after the password check so failed
            # attempts do not mutate account metadata.
            cur.execute(
                queries.get("update_user_last_login"),
                {"user_id": auth_row["user_id"]},
            )
            user_row = cur.fetchone()

    logger.info("Login succeeded user_id=%s.", auth_row["user_id"])
    return {
        # Tokens are stateless JWTs. The client stores and presents this value
        # in `Authorization: Bearer ...` for control-plane requests.
        "access_token": create_access_token(
            str(auth_row["user_id"]),
            token_version=int(auth_row["token_version"]),
        ),
        "token_type": "bearer",
        "user": serialize_user(user_row),
    }

def logout(user_id: Any) -> dict[str, bool]:
    """Invalidate existing user access tokens by advancing token_version."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("increment_user_token_version"),
                {"user_id": user_id},
            )
            row = cur.fetchone()
    if row is None:
        raise ApiError(
            type="user_not_found",
            message="User not found.",
            status_code=404,
        )
    logger.info("Logout revoked access tokens user_id=%s.", user_id)
    return {"logged_out": True}


def invalid_credentials_error() -> ApiError:
    """Build the standard login failure error without account enumeration."""
    return ApiError(
        type="invalid_credentials",
        message="Invalid email or password.",
        status_code=401,
    )
