"""Control-plane idempotency logic.

Idempotency keys let dashboard/CLI clients safely retry side-effecting
control-plane requests. A repeated key with the same request replays the saved
response; a repeated key with different request content is rejected.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from app.db.pool import transaction
from app.db.sql import load_queries
from app.utils.errors import ApiError
from app.utils.time import utc_now_plus
from app.utils.validation import validate_string, validate_uuid


queries = load_queries()
IDEMPOTENCY_KEY_MAX_LENGTH = 255
IDEMPOTENCY_TTL_HOURS = 24


def run_idempotent_control_plane_request(
    *,
    project_id: Any,
    user_id: Any,
    idempotency_key: str | None,
    method: str,
    path: str,
    body: Any,
    action: Callable[[], tuple[dict[str, Any], int]],
) -> tuple[dict[str, Any], int]:
    """Run a control-plane action with required idempotency protection.

    The request is hashed before the action runs, and the successful response is
    saved for exact retries.
    """
    if not idempotency_key:
        raise missing_idempotency_key_error()

    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_key = validate_string(
        idempotency_key,
        "Idempotency-Key",
        max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    request_hash = build_request_hash(method=method, path=path, body=body)

    record = get_or_create_idempotency_record(
        project_id=canonical_project_id,
        user_id=canonical_user_id,
        idempotency_key=canonical_key,
        request_hash=request_hash,
    )

    if record["request_hash"] != request_hash:
        raise ApiError(
            type="idempotency_key_conflict",
            message="This Idempotency-Key was already used with a different request.",
            status_code=409,
        )

    if record["response_status"] is not None and record["response_body"] is not None:
        return record["response_body"], record["response_status"]

    response_body, response_status = action()
    save_idempotency_response(
        record["idempotency_key_id"],
        response_status=response_status,
        response_body=response_body,
    )
    return response_body, response_status


def build_request_hash(*, method: str, path: str, body: Any) -> str:
    """Build a stable hash for the HTTP operation protected by a key."""
    normalized = {
        "method": method.upper(),
        "path": path,
        "body": body if body is not None else {},
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def missing_idempotency_key_error() -> ApiError:
    """Build the standard missing Idempotency-Key error."""
    return ApiError(
        type="missing_idempotency_key",
        message="Idempotency-Key header is required for this operation.",
        status_code=400,
    )


def get_or_create_idempotency_record(
    *,
    project_id: str,
    user_id: str,
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any]:
    """Return an existing idempotency row or create a new pending row."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("get_idempotency_key"),
                {
                    "project_id": project_id,
                    "user_id": user_id,
                    "idempotency_key": idempotency_key,
                },
            )
            record = cur.fetchone()

            if record is not None:
                return record

            try:
                cur.execute(
                    queries.get("create_idempotency_key"),
                    {
                        "project_id": project_id,
                        "user_id": user_id,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "response_status": None,
                        "response_body": None,
                        "expires_at": utc_now_plus(hours=IDEMPOTENCY_TTL_HOURS),
                    },
                )
            except Exception as exc:
                if _is_unique_violation(exc):
                    raise ApiError(
                        type="idempotency_key_in_progress",
                        message=(
                            "This Idempotency-Key is already being processed. "
                            "Please retry shortly."
                        ),
                        status_code=409,
                    ) from exc
                raise

            return cur.fetchone()


def save_idempotency_response(
    idempotency_key_id: Any,
    *,
    response_status: int,
    response_body: dict[str, Any],
) -> None:
    """Store the successful response that future exact retries should replay."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("update_idempotency_response"),
                {
                    "idempotency_key_id": idempotency_key_id,
                    "response_status": response_status,
                    "response_body": _jsonb(response_body),
                },
            )


def _is_unique_violation(exc: Exception) -> bool:
    """Detect psycopg unique violations without importing psycopg globally."""
    return exc.__class__.__name__ == "UniqueViolation"


def _jsonb(value: dict[str, Any]) -> Any:
    """Wrap a value as JSONB when psycopg is installed; return raw value in tests."""
    try:
        from psycopg.types.json import Jsonb
    except ModuleNotFoundError:
        return value

    return Jsonb(value)
