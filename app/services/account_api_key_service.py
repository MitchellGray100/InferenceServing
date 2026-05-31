"""Account API key business logic.

Account keys are user-scoped credentials for automation such as Truss. They are
managed from the account page and are not accepted by inference routes.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.pool import transaction
from app.db.sql import load_queries
from app.security import api_keys
from app.utils.errors import ApiError
from app.utils.time import to_iso8601
from app.utils.validation import validate_api_key_name, validate_uuid


queries = load_queries()
logger = logging.getLogger(__name__)
ACCOUNT_API_KEY_HASH_UNIQUE_CONSTRAINT = "uq_account_api_keys_key_hash"
ACCOUNT_API_KEY_NAME_UNIQUE_CONSTRAINT = "uq_account_api_keys_user_active_name"
MAX_API_KEY_CREATE_ATTEMPTS = 3


def create_account_api_key(user_id: Any, name: Any) -> dict[str, Any]:
    """Create an account API key and return the raw key once."""
    canonical_user_id = validate_uuid(user_id, "userID")
    key_name = validate_api_key_name(name)

    row = None
    raw_key = None
    for attempt in range(MAX_API_KEY_CREATE_ATTEMPTS):
        raw_key, key_prefix = api_keys.generate_api_key()
        key_hash = api_keys.hash_api_key(raw_key)

        try:
            with transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        queries.get("create_account_api_key"),
                        {
                            "user_id": canonical_user_id,
                            "name": key_name,
                            "key_prefix": key_prefix,
                            "key_hash": key_hash,
                        },
                    )
                    row = cur.fetchone()
                    break
        except Exception as exc:
            if _is_unique_violation(exc, ACCOUNT_API_KEY_NAME_UNIQUE_CONSTRAINT):
                raise ApiError(
                    type="validation_error",
                    message="An account API key with that name already exists.",
                    status_code=409,
                ) from exc

            if _is_unique_violation(exc, ACCOUNT_API_KEY_HASH_UNIQUE_CONSTRAINT):
                if attempt < MAX_API_KEY_CREATE_ATTEMPTS - 1:
                    logger.warning(
                        "Account API key hash collision user_id=%s attempt=%s.",
                        canonical_user_id,
                        attempt + 1,
                    )
                    continue
                raise ApiError(
                    type="api_key_generation_failed",
                    message="Could not generate a unique API key. Please try again.",
                    status_code=500,
                ) from exc

            raise

    if row is None or raw_key is None:
        raise ApiError(
            type="api_key_generation_failed",
            message="Could not generate a unique API key. Please try again.",
            status_code=500,
        )

    logger.info(
        "Created account API key account_api_key_id=%s user_id=%s.",
        row["account_api_key_id"],
        canonical_user_id,
    )
    response = serialize_account_api_key(row)
    response["api_key"] = raw_key
    return response


def list_account_api_keys(user_id: Any) -> dict[str, list[dict[str, Any]]]:
    """List account API key metadata for the authenticated user."""
    canonical_user_id = validate_uuid(user_id, "userID")

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("list_account_api_keys"),
                {"user_id": canonical_user_id},
            )
            rows = cur.fetchall()

    return {"account_api_keys": [serialize_account_api_key(row) for row in rows]}


def revoke_account_api_key(user_id: Any, account_api_key_id: Any) -> dict[str, bool]:
    """Revoke one account API key owned by the authenticated user."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_key_id = validate_uuid(account_api_key_id, "accountApiKeyID")

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("revoke_account_api_key"),
                {
                    "user_id": canonical_user_id,
                    "account_api_key_id": canonical_key_id,
                },
            )
            row = cur.fetchone()

    if row is None:
        raise account_api_key_not_found_error()
    return {"revoked": True}


def authenticate_account_api_key(raw_key: str) -> dict[str, str]:
    """Verify a raw account API key and return its owning user identity."""
    key_prefix = api_keys.derive_key_prefix(raw_key)

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("find_active_account_api_keys_by_prefix"),
                {"key_prefix": key_prefix},
            )
            rows = cur.fetchall()

            for row in rows:
                if api_keys.verify_api_key(raw_key, row["key_hash"]):
                    cur.execute(
                        queries.get("update_account_api_key_last_used"),
                        {"account_api_key_id": row["account_api_key_id"]},
                    )
                    return {
                        "accountApiKeyID": str(row["account_api_key_id"]),
                        "userID": str(row["user_id"]),
                        "apiKeyPrefix": row["key_prefix"],
                    }

    raise api_keys.invalid_api_key_error()


def serialize_account_api_key(row: Any) -> dict[str, Any]:
    """Serialize account API key metadata without exposing key material."""
    return {
        "accountApiKeyID": str(row["account_api_key_id"]),
        "userID": str(row["user_id"]),
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "created_at": to_iso8601(row["created_at"]),
        "last_used_at": _optional_iso8601(row["last_used_at"]),
        "revoked_at": _optional_iso8601(row["revoked_at"]),
    }


def account_api_key_not_found_error() -> ApiError:
    """Build the standard account API key not found response."""
    return ApiError(
        type="account_api_key_not_found",
        message="Account API key not found.",
        status_code=404,
    )


def _optional_iso8601(value: Any) -> str | None:
    return to_iso8601(value) if value is not None else None


def _constraint_name(exc: Exception) -> str | None:
    diag = getattr(exc, "diag", None)
    return getattr(diag, "constraint_name", None)


def _is_unique_violation(exc: Exception, constraint_name: str) -> bool:
    if exc.__class__.__name__ != "UniqueViolation":
        return False
    return _constraint_name(exc) == constraint_name
