"""Project API key business logic.

Control-plane users manage project API keys through this service. The raw key
is returned only from `create_api_key`; all later reads expose metadata only.
"""

from __future__ import annotations

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


queries = load_queries()


def create_api_key(user_id: Any, project_id: Any, name: Any) -> dict[str, Any]:
    """Create a project API key for owners and members."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    key_name = validate_api_key_name(name)
    raw_key, key_prefix = api_keys.generate_api_key()
    key_hash = api_keys.hash_api_key(raw_key)

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, WRITE_ROLES)

            try:
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
            except Exception as exc:
                if _is_unique_violation(exc):
                    raise ApiError(
                        type="validation_error",
                        message="An API key with that name already exists.",
                        status_code=409,
                    ) from exc
                raise

            row = cur.fetchone()

    response = serialize_api_key(row)
    response["api_key"] = raw_key
    return response


def list_api_keys(user_id: Any, project_id: Any) -> dict[str, list[dict[str, Any]]]:
    """List project API key metadata for any project member."""
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

    return {"api_keys": [serialize_api_key(row) for row in rows]}


def revoke_api_key(user_id: Any, project_id: Any, api_key_id: Any) -> dict[str, bool]:
    """Revoke a project API key for owners and members."""
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
        raise api_key_not_found_error()

    return {"revoked": True}


def authenticate_project_api_key(raw_key: str) -> dict[str, str]:
    """Verify a raw project API key and return its project/key identity.

    This helper is intended for the inference routes. It looks up active key
    candidates by visible prefix, verifies HMACs in constant time, and records
    successful use.
    """
    key_prefix = api_keys.derive_key_prefix(raw_key)

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("find_active_api_keys_by_prefix"),
                {"key_prefix": key_prefix},
            )
            rows = cur.fetchall()

            for row in rows:
                if api_keys.verify_api_key(raw_key, row["key_hash"]):
                    cur.execute(
                        queries.get("update_api_key_last_used"),
                        {"api_key_id": row["api_key_id"]},
                    )
                    return {
                        "apiKeyID": str(row["api_key_id"]),
                        "projectID": str(row["project_id"]),
                    }

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
    return to_iso8601(value) if value is not None else None


def _is_unique_violation(exc: Exception) -> bool:
    return exc.__class__.__name__ == "UniqueViolation"
