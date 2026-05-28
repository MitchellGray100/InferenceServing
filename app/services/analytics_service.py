"""Analytics service logic.

This module backs the dashboard analytics endpoints. It reads only lightweight
metadata from `inference_requests` and `model_events`; prompts and generated
responses are intentionally not persisted or returned by the MVP.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.db.pool import transaction
from app.db.sql import load_queries
from app.services.model_deployment_service import model_deployment_not_found_error
from app.services.project_service import (
    VIEW_ROLES,
    get_project_role_with_cursor,
    require_role,
)
from app.utils.errors import ValidationError
from app.utils.time import to_iso8601
from app.utils.validation import (
    validate_deployment_name,
    validate_positive_int,
    validate_uuid,
)


DEFAULT_REQUEST_LIMIT = 100
MAX_REQUEST_LIMIT = 500
queries = load_queries()
logger = logging.getLogger(__name__)


def get_model_metrics(
    user_id: Any,
    project_id: Any,
    model_name: Any,
    since: Any = None,
) -> dict[str, Any]:
    """Return aggregate inference metrics for one named model deployment."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_model_name = validate_deployment_name(model_name)
    since_timestamp = parse_optional_iso8601(since, "since")

    with transaction() as conn:
        with conn.cursor() as cur:
            # Viewers may read analytics, but non-members should see the same
            # project-not-found response used elsewhere to avoid leaking IDs.
            require_project_view_role(cur, canonical_project_id, canonical_user_id)
            deployment = get_model_deployment_by_name_with_cursor(
                cur,
                canonical_project_id,
                canonical_model_name,
            )

            if deployment is None:
                raise model_deployment_not_found_error()

            # The aggregation is done in Postgres so the service only needs to
            # format the one-row result into the public response shape.
            cur.execute(
                queries.get("get_model_inference_metrics"),
                {
                    "model_deployment_id": deployment["model_deployment_id"],
                    "since": since_timestamp,
                },
            )
            metrics = cur.fetchone()

    logger.debug(
        "Fetched model metrics project_id=%s model=%s.",
        canonical_project_id,
        canonical_model_name,
    )
    return {
        "model": serialize_model_summary(deployment),
        "metrics": serialize_model_metrics(metrics),
    }


def list_model_requests(
    user_id: Any,
    project_id: Any,
    model_name: Any,
    *,
    limit: Any = None,
    status_code: Any = None,
    since: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return recent inference request metadata for one named model."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_model_name = validate_deployment_name(model_name)
    request_limit = parse_optional_positive_int(
        limit,
        "limit",
        default=DEFAULT_REQUEST_LIMIT,
        max_value=MAX_REQUEST_LIMIT,
    )
    response_status_code = parse_optional_positive_int(
        status_code,
        "status_code",
        default=None,
        min_value=100,
        max_value=599,
    )
    since_timestamp = parse_optional_iso8601(since, "since")

    with transaction() as conn:
        with conn.cursor() as cur:
            require_project_view_role(cur, canonical_project_id, canonical_user_id)
            deployment = get_model_deployment_by_name_with_cursor(
                cur,
                canonical_project_id,
                canonical_model_name,
            )

            if deployment is None:
                raise model_deployment_not_found_error()

            # Request rows are metadata-only. This endpoint deliberately does
            # not include request or response payloads.
            cur.execute(
                queries.get("list_recent_inference_requests"),
                {
                    "model_deployment_id": deployment["model_deployment_id"],
                    "limit": request_limit,
                    "status_code": response_status_code,
                    "since": since_timestamp,
                },
            )
            rows = cur.fetchall()

    logger.debug(
        "Listed model request history project_id=%s model=%s count=%s.",
        canonical_project_id,
        canonical_model_name,
        len(rows),
    )
    return {"requests": [serialize_inference_request(row) for row in rows]}


def list_model_events(
    user_id: Any,
    project_id: Any,
    model_name: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Return lifecycle events for one named model deployment."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_model_name = validate_deployment_name(model_name)

    with transaction() as conn:
        with conn.cursor() as cur:
            require_project_view_role(cur, canonical_project_id, canonical_user_id)
            deployment = get_model_deployment_by_name_with_cursor(
                cur,
                canonical_project_id,
                canonical_model_name,
            )

            if deployment is None:
                raise model_deployment_not_found_error()

            cur.execute(
                queries.get("list_model_events"),
                {"model_deployment_id": deployment["model_deployment_id"]},
            )
            rows = cur.fetchall()

    logger.debug(
        "Listed model events project_id=%s model=%s count=%s.",
        canonical_project_id,
        canonical_model_name,
        len(rows),
    )
    return {"events": [serialize_model_event(row) for row in rows]}


def require_project_view_role(cur: Any, project_id: str, user_id: str) -> None:
    """Require project membership with permission to view analytics."""
    role = get_project_role_with_cursor(cur, project_id, user_id)
    require_role(role, VIEW_ROLES)


def get_model_deployment_by_name_with_cursor(
    cur: Any,
    project_id: str,
    model_name: str,
) -> Any:
    """Fetch one non-deleted model deployment by its project-local name."""
    cur.execute(
        queries.get("get_model_deployment_by_name"),
        {
            "project_id": project_id,
            "name": model_name,
        },
    )
    return cur.fetchone()


def serialize_model_summary(row: Any) -> dict[str, Any]:
    """Serialize the compact model object used by analytics responses."""
    return {
        "name": row["name"],
        "model_id": row["model_id"],
        "status": row["status"],
    }


def serialize_model_metrics(row: Any | None) -> dict[str, Any]:
    """Serialize aggregate metrics, defaulting empty aggregates to zeroes."""
    row = row or {}

    return {
        "request_count": row.get("request_count") or 0,
        "success_count": row.get("success_count") or 0,
        "error_count": row.get("error_count") or 0,
        "average_latency_ms": row.get("average_latency_ms"),
        "p95_latency_ms": row.get("p95_latency_ms"),
        "last_request_at": optional_iso8601(row.get("last_request_at")),
    }


def serialize_inference_request(row: Any) -> dict[str, Any]:
    """Serialize one inference request metadata row."""
    return {
        "inferenceRequestID": str(row["inference_request_id"]),
        "projectID": str(row["project_id"]),
        "modelDeploymentID": str(row["model_deployment_id"]),
        "apiKeyID": str(row["api_key_id"]) if row["api_key_id"] is not None else None,
        "status_code": row["status_code"],
        "latency_ms": row["latency_ms"],
        "error_type": row["error_type"],
        "request_path": row["request_path"],
        "method": row["method"],
        "streamed": row["streamed"],
        "created_at": to_iso8601(row["created_at"]),
    }


def serialize_model_event(row: Any) -> dict[str, Any]:
    """Serialize one model lifecycle event row."""
    return {
        "modelEventID": str(row["model_event_id"]),
        "modelDeploymentID": str(row["model_deployment_id"]),
        "projectID": str(row["project_id"]),
        "event_type": row["event_type"],
        "message": row["message"],
        "metadata": row["metadata"] or {},
        "created_at": to_iso8601(row["created_at"]),
    }


def parse_optional_positive_int(
    value: Any,
    field: str,
    *,
    default: int | None,
    min_value: int = 1,
    max_value: int | None = None,
) -> int | None:
    """Parse optional integer query parameters before range validation."""
    if value is None or value == "":
        return default

    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{field} must be an integer.",
            details={"field": field},
        ) from exc

    return validate_positive_int(
        parsed,
        field,
        min_value=min_value,
        max_value=max_value,
    )


def parse_optional_iso8601(value: Any, field: str) -> datetime | None:
    """Parse optional ISO-8601 timestamps from analytics query strings."""
    if value is None or value == "":
        return None

    if not isinstance(value, str):
        raise ValidationError(
            f"{field} must be an ISO-8601 timestamp.",
            details={"field": field},
        )

    # Python's parser accepts offsets like +00:00 but not a trailing Z, so
    # normalize the common UTC form used by API examples before parsing.
    normalized = value.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError(
            f"{field} must be an ISO-8601 timestamp.",
            details={"field": field},
        ) from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)

    return parsed.astimezone(UTC)


def optional_iso8601(value: Any) -> str | None:
    """Serialize optional timestamps in analytics responses."""
    return to_iso8601(value) if value is not None else None
