"""Project API key business logic.

Control-plane users manage project API keys through this service. The raw key
is returned only from `create_api_key`; all later reads expose metadata only.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.pool import transaction
from app.db.sql import load_queries
from app.security import api_keys
from app.services.project_service import (
    VIEW_ROLES,
    WRITE_ROLES,
    get_project_role_with_cursor,
    require_role,
)
from app.utils.errors import ApiError
from app.utils.time import to_iso8601
from app.utils.validation import validate_api_key_name, validate_uuid


# API key queries intentionally never return `key_hash` except on the dedicated
# verification lookup. List/create/revoke responses expose metadata only.
queries = load_queries()
logger = logging.getLogger(__name__)
API_KEY_HASH_UNIQUE_CONSTRAINT = "uq_api_keys_key_hash"
API_KEY_NAME_UNIQUE_CONSTRAINTS = {
    "uq_api_keys_project_name",
    "uq_api_keys_project_active_name",
}
MAX_API_KEY_CREATE_ATTEMPTS = 3


def create_api_key(user_id: Any, project_id: Any, name: Any) -> dict[str, Any]:
    """Create a project API key for owners and members.

    The raw key is generated before the transaction but only returned after the
    metadata row commits. The database stores a visible prefix and HMAC; it does
    not store enough information to reconstruct the raw key.
    """
    # Validate all caller-controlled identifiers before generating/storing the
    # credential metadata.
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    key_name = validate_api_key_name(name)

    row = None
    raw_key = None

    for attempt in range(MAX_API_KEY_CREATE_ATTEMPTS):
        # Generate a new raw key for each attempt. Only the visible prefix and
        # HMAC are persisted; the raw key is returned only after the insert wins.
        raw_key, key_prefix = api_keys.generate_api_key()
        key_hash = api_keys.hash_api_key(raw_key)

        try:
            with transaction() as conn:
                with conn.cursor() as cur:
                    # Project membership is checked in the same transaction as key
                    # creation so permission and insert cannot drift apart.
                    role = get_project_role_with_cursor(
                        cur,
                        canonical_project_id,
                        canonical_user_id,
                    )
                    require_role(role, WRITE_ROLES)

                    cur.execute(
                        queries.get("create_api_key"),
                        {
                            "project_id": canonical_project_id,
                            "name": key_name,
                            "key_prefix": key_prefix,
                            "key_hash": key_hash,
                            "created_by_user_id": canonical_user_id,
                        },
                    )

                    row = cur.fetchone()
                    break
        except Exception as exc:
            if _is_unique_violation(exc, API_KEY_NAME_UNIQUE_CONSTRAINTS):
                logger.info(
                    "API key creation rejected due to duplicate name project_id=%s.",
                    canonical_project_id,
                )
                raise ApiError(
                    type="validation_error",
                    message="An API key with that name already exists.",
                    status_code=409,
                ) from exc

            if _is_unique_violation(exc, API_KEY_HASH_UNIQUE_CONSTRAINT):
                if attempt < MAX_API_KEY_CREATE_ATTEMPTS - 1:
                    logger.warning(
                        "API key hash collision detected project_id=%s attempt=%s.",
                        canonical_project_id,
                        attempt + 1,
                    )
                    continue

                logger.error(
                    "API key generation exhausted hash collision retries project_id=%s.",
                    canonical_project_id,
                )
                raise ApiError(
                    type="api_key_generation_failed",
                    message="Could not generate a unique API key. Please try again.",
                    status_code=500,
                ) from exc

            raise

    if row is None or raw_key is None:
        logger.error("API key generation failed project_id=%s.", canonical_project_id)
        raise ApiError(
            type="api_key_generation_failed",
            message="Could not generate a unique API key. Please try again.",
            status_code=500,
        )


    logger.info(
        "Created API key api_key_id=%s project_id=%s.",
        row["api_key_id"],
        canonical_project_id,
    )
    response = serialize_api_key(row)

    # This is the only API response that includes the raw credential. All later
    # reads use `serialize_api_key`, which deliberately omits `key_hash` and
    # cannot recover `api_key`.
    response["api_key"] = raw_key
    return response


def list_api_keys(user_id: Any, project_id: Any) -> dict[str, list[dict[str, Any]]]:
    """List project API key metadata for any project member."""
    # Viewers can inspect key metadata, but no read path can recover raw keys.
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, VIEW_ROLES)
            cur.execute(
                queries.get("list_api_keys"),
                {"project_id": canonical_project_id},
            )
            rows = cur.fetchall()

    logger.debug(
        "Listed API keys project_id=%s count=%s.",
        canonical_project_id,
        len(rows),
    )
    return {"api_keys": [serialize_api_key(row) for row in rows]}


def revoke_api_key(user_id: Any, project_id: Any, api_key_id: Any) -> dict[str, bool]:
    """Revoke a project API key for owners and members.

    Revocation is soft-delete style: `revoked_at` is set so historical
    inference rows can still reference the key metadata.
    """
    # Revocation is authorized against the project first, then scoped to the
    # key ID inside that project.
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_api_key_id = validate_uuid(api_key_id, "apiKeyID")

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, WRITE_ROLES)
            cur.execute(
                queries.get("revoke_api_key"),
                {
                    "project_id": canonical_project_id,
                    "api_key_id": canonical_api_key_id,
                },
            )
            row = cur.fetchone()

    if row is None:
        logger.info(
            "API key revoke missed api_key_id=%s project_id=%s.",
            canonical_api_key_id,
            canonical_project_id,
        )
        raise api_key_not_found_error()

    logger.info(
        "Revoked API key api_key_id=%s project_id=%s.",
        canonical_api_key_id,
        canonical_project_id,
    )
    return {"revoked": True}


def authenticate_project_api_key(raw_key: str) -> dict[str, str]:
    """Verify a raw project API key and return its project/key identity.

    This helper is intended for the inference routes. It looks up active key
    candidates by visible prefix, verifies HMACs in constant time, and records
    successful use.
    """
    # Prefix lookup narrows the candidate set without treating the prefix as a
    # secret. The full raw key still has to match the stored HMAC.
    key_prefix = api_keys.derive_key_prefix(raw_key)

    with transaction() as conn:
        with conn.cursor() as cur:
            # A prefix can theoretically match multiple active keys, so each
            # candidate still goes through constant-time HMAC verification.
            cur.execute(
                queries.get("find_active_api_keys_by_prefix"),
                {"key_prefix": key_prefix},
            )
            rows = cur.fetchall()

            for row in rows:
                if api_keys.verify_api_key(raw_key, row["key_hash"]):
                    # Update last_used_at only after a real match. Failed key
                    # attempts do not mutate key metadata.
                    cur.execute(
                        queries.get("update_api_key_last_used"),
                        {"api_key_id": row["api_key_id"]},
                    )
                    logger.debug(
                        "Authenticated project API key api_key_id=%s project_id=%s.",
                        row["api_key_id"],
                        row["project_id"],
                    )
                    return {
                        "apiKeyID": str(row["api_key_id"]),
                        "projectID": str(row["project_id"]),
                        "apiKeyPrefix": row["key_prefix"],
                    }

    logger.info("Project API key authentication failed for prefix=%s.", key_prefix)
    raise api_keys.invalid_api_key_error()


def serialize_api_key(row: Any) -> dict[str, Any]:
    """Serialize API key metadata without exposing `key_hash`."""
    return {
        "apiKeyID": str(row["api_key_id"]),
        "projectID": str(row["project_id"]),
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "created_at": to_iso8601(row["created_at"]),
        "last_used_at": _optional_iso8601(row["last_used_at"]),
        "revoked_at": _optional_iso8601(row["revoked_at"]),
    }


def api_key_not_found_error() -> ApiError:
    """Build the standard API key not found response."""
    return ApiError(
        type="api_key_not_found",
        message="API key not found.",
        status_code=404,
    )


def _optional_iso8601(value: Any) -> str | None:
    """Serialize optional API key timestamps."""
    return to_iso8601(value) if value is not None else None


def _constraint_name(exc: Exception) -> str | None:
    """Return a database constraint name from a psycopg-style exception."""
    diag = getattr(exc, "diag", None)
    return getattr(diag, "constraint_name", None)


def _is_unique_violation(
    exc: Exception,
    constraint_name: str | set[str] | None = None,
) -> bool:
    """Detect psycopg unique violations without importing psycopg globally."""
    if exc.__class__.__name__ != "UniqueViolation":
        return False

    if constraint_name is None:
        return True

    actual = _constraint_name(exc)
    if isinstance(constraint_name, set):
        return actual in constraint_name
    return actual == constraint_name
