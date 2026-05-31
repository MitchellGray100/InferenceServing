"""Project business logic.

This service creates projects, generates unique slugs/namespaces, and manages
project-level authorization checks.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Config
from app.db.pool import transaction
from app.db.sql import load_queries
from app.utils.errors import ApiError
from app.utils.time import to_iso8601
from app.utils.validation import (
    normalize_email,
    slugify,
    validate_project_name,
    validate_role,
    validate_uuid,
)


# Project role sets are reused by other services so authorization rules stay
# consistent across projects, API keys, and model deployments.
queries = load_queries()
logger = logging.getLogger(__name__)
WRITE_ROLES = {"owner", "member"}
VIEW_ROLES = {"owner", "member", "viewer"}


def create_project(user_id: Any, name: Any) -> dict[str, Any]:
    """Create a project and make the current user its owner."""
    canonical_user_id = validate_uuid(user_id, "userID")
    project_name = validate_project_name(name)
    base_slug = slugify(project_name)

    if not base_slug:
        raise ApiError(
            type="validation_error",
            message="Project name must contain at least one letter or number.",
            status_code=400,
        )

    with transaction() as conn:
        with conn.cursor() as cur:
            # Project creation and owner membership must commit together. A
            # project without an owner would be inaccessible.
            project_row = _insert_project_with_unique_slug(cur, project_name, base_slug)
            cur.execute(
                queries.get("create_project_member"),
                {
                    "project_id": project_row["project_id"],
                    "user_id": canonical_user_id,
                    "role": "owner",
                },
            )

    logger.info(
        "Created project project_id=%s owner_user_id=%s.",
        project_row["project_id"],
        canonical_user_id,
    )
    return serialize_project(project_row, role="owner")


def list_projects(user_id: Any) -> dict[str, list[dict[str, Any]]]:
    """List projects the current user belongs to."""
    canonical_user_id = validate_uuid(user_id, "userID")

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("list_projects_for_user"),
                {"user_id": canonical_user_id},
            )
            rows = cur.fetchall()

    logger.debug("Listed projects user_id=%s count=%s.", canonical_user_id, len(rows))
    return {"projects": [serialize_project(row, role=row["role"]) for row in rows]}


def get_project(user_id: Any, project_id: Any) -> dict[str, Any]:
    """Return one project if the user is a member."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("get_project_for_user"),
                {
                    "project_id": canonical_project_id,
                    "user_id": canonical_user_id,
                },
            )
            row = cur.fetchone()

    if row is None:
        logger.info(
            "Project lookup missed project_id=%s user_id=%s.",
            canonical_project_id,
            canonical_user_id,
        )
        raise project_not_found_error()

    logger.debug("Fetched project project_id=%s.", canonical_project_id)
    return serialize_project(row, role=row["role"])


def get_project_by_name(user_id: Any, name: Any) -> dict[str, Any]:
    """Return one project by name if the user is a member."""
    canonical_user_id = validate_uuid(user_id, "userID")
    project_name = validate_project_name(name)

    with transaction() as conn:
        with conn.cursor() as cur:
            project = get_project_by_name_for_user_with_cursor(
                cur,
                project_name,
                canonical_user_id,
            )

    if project is None:
        logger.info(
            "Project name lookup missed name=%s user_id=%s.",
            project_name,
            canonical_user_id,
        )
        raise project_not_found_error()
    return serialize_project(project, role=project["role"])


def create_project_if_missing(user_id: Any, name: Any) -> dict[str, Any]:
    """Return a user's project by name, creating it when absent."""
    try:
        return get_project_by_name(user_id, name)
    except ApiError as exc:
        if exc.type != "project_not_found":
            raise
    return create_project(user_id, name)


def delete_project(user_id: Any, project_id: Any) -> dict[str, bool]:
    """Delete a project when the current user is an owner.

    The project owns a Kubernetes namespace. The API deletes product metadata
    immediately and queues namespace cleanup for the deployment worker so slow
    Kubernetes operations do not block the request.
    """
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, {"owner"})
            project = get_project_for_user_with_cursor(
                cur,
                canonical_project_id,
                canonical_user_id,
            )

    if project is None:
        logger.info("Project delete missed project_id=%s.", canonical_project_id)
        raise project_not_found_error()

    with transaction() as conn:
        with conn.cursor() as cur:
            enqueue_project_cleanup_job_with_cursor(cur, project)
            cur.execute(
                queries.get("delete_project"),
                {"project_id": canonical_project_id},
            )
            row = cur.fetchone()

    if row is None:
        logger.info("Project delete missed project_id=%s.", canonical_project_id)
        raise project_not_found_error()

    logger.info(
        "Deleted project project_id=%s user_id=%s.",
        canonical_project_id,
        canonical_user_id,
    )
    return {"deleted": True}


def delete_sole_owner_projects_for_user(user_id: Any) -> list[dict[str, Any]]:
    """Delete projects that would have no owner after deleting this user.

    Namespace cleanup is queued before database rows are deleted, so account
    deletion does not leave project Kubernetes resources without a retryable
    cleanup record.
    """
    canonical_user_id = validate_uuid(user_id, "userID")

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("list_sole_owner_projects_for_user"),
                {"user_id": canonical_user_id},
            )
            projects = cur.fetchall()

    deleted_projects: list[dict[str, Any]] = []
    with transaction() as conn:
        with conn.cursor() as cur:
            for project in projects:
                # The cleanup job must be inserted in the same transaction as
                # metadata deletion. If the transaction rolls back, neither the
                # project row nor its retryable cleanup record is lost.
                enqueue_project_cleanup_job_with_cursor(cur, project)
                cur.execute(
                    queries.get("delete_project"),
                    {"project_id": project["project_id"]},
                )
                row = cur.fetchone()
                if row is not None:
                    deleted_projects.append(serialize_project(project, role=project["role"]))

    if deleted_projects:
        logger.info(
            "Deleted sole-owner projects for user_id=%s count=%s.",
            canonical_user_id,
            len(deleted_projects),
        )

    return deleted_projects


def enqueue_project_cleanup_job_with_cursor(cur: Any, project: Any) -> Any:
    """Insert a durable namespace cleanup job before deleting project metadata."""
    # The cleanup table intentionally has no FK to projects because this row
    # must outlive the project metadata long enough for the worker to delete
    # the Kubernetes namespace.
    cur.execute(
        queries.get("create_project_cleanup_job"),
        {
            "project_id": project["project_id"],
            "k8s_namespace": project["k8s_namespace"],
        },
    )
    job = cur.fetchone()
    logger.debug(
        "Queued project cleanup job job_id=%s project_id=%s namespace=%s.",
        job["project_cleanup_job_id"],
        project["project_id"],
        project["k8s_namespace"],
    )
    return job


def list_project_members(user_id: Any, project_id: Any) -> dict[str, list[dict[str, Any]]]:
    """List project members when the current user can view the project."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, VIEW_ROLES)
            cur.execute(
                queries.get("list_project_members"),
                {"project_id": canonical_project_id},
            )
            rows = cur.fetchall()

    logger.debug(
        "Listed project members project_id=%s count=%s.",
        canonical_project_id,
        len(rows),
    )
    return {"members": [serialize_member(row) for row in rows]}


def add_project_member(
    user_id: Any,
    project_id: Any,
    email: Any,
    role: Any,
) -> dict[str, Any]:
    """Add an existing user to a project.

    The MVP does not implement invitations, so the target user must already
    exist before they can be added by email.
    """
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    normalized_email = normalize_email(email)
    member_role = validate_role(role)

    with transaction() as conn:
        with conn.cursor() as cur:
            current_role = get_project_role_with_cursor(
                cur,
                canonical_project_id,
                canonical_user_id,
            )
            require_role(current_role, {"owner"})
            cur.execute(
                queries.get("get_user_by_email"),
                {"email": normalized_email},
            )
            target_user = cur.fetchone()

            if target_user is None:
                logger.info(
                    "Add project member missed target email project_id=%s.",
                    canonical_project_id,
                )
                raise ApiError(
                    type="user_not_found",
                    message="No user exists with that email.",
                    status_code=404,
                )

            try:
                cur.execute(
                    queries.get("create_project_member"),
                    {
                        "project_id": canonical_project_id,
                        "user_id": target_user["user_id"],
                        "role": member_role,
                    },
                )
            except Exception as exc:
                if _is_unique_violation(exc):
                    logger.info(
                        "Add project member rejected duplicate project_id=%s target_user_id=%s.",
                        canonical_project_id,
                        target_user["user_id"],
                    )
                    raise ApiError(
                        type="validation_error",
                        message="User is already a member of this project.",
                        status_code=409,
                    ) from exc
                raise

            membership = cur.fetchone()

    logger.info(
        "Added project member project_id=%s target_user_id=%s role=%s.",
        canonical_project_id,
        target_user["user_id"],
        member_role,
    )
    return serialize_member({**target_user, "role": membership["role"], "created_at": membership["created_at"]})


def update_project_member_role(
    user_id: Any,
    project_id: Any,
    target_user_id: Any,
    role: Any,
) -> dict[str, Any]:
    """Update a project member role while protecting the last owner."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_target_user_id = validate_uuid(target_user_id, "userID")
    new_role = validate_role(role)

    with transaction() as conn:
        with conn.cursor() as cur:
            current_role = get_project_role_with_cursor(
                cur,
                canonical_project_id,
                canonical_user_id,
            )
            require_role(current_role, {"owner"})
            existing_member = get_project_member_with_cursor(
                cur,
                canonical_project_id,
                canonical_target_user_id,
            )

            if existing_member is None:
                logger.info(
                    "Update project member missed project_id=%s target_user_id=%s.",
                    canonical_project_id,
                    canonical_target_user_id,
                )
                raise ApiError(
                    type="user_not_found",
                    message="User is not a member of this project.",
                    status_code=404,
                )

            # A project must always retain at least one owner. Downgrading the
            # only owner would leave the project without an administrator.
            if existing_member["role"] == "owner" and new_role != "owner":
                ensure_not_last_owner(cur, canonical_project_id)

            cur.execute(
                queries.get("update_project_member_role"),
                {
                    "project_id": canonical_project_id,
                    "user_id": canonical_target_user_id,
                    "role": new_role,
                },
            )
            updated_membership = cur.fetchone()

    logger.info(
        "Updated project member role project_id=%s target_user_id=%s role=%s.",
        canonical_project_id,
        canonical_target_user_id,
        new_role,
    )
    return serialize_member(
        {
            **existing_member,
            "role": updated_membership["role"],
            "created_at": updated_membership["created_at"],
        }
    )


def remove_project_member(
    user_id: Any,
    project_id: Any,
    target_user_id: Any,
) -> dict[str, bool]:
    """Remove a project member while protecting the last owner."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_target_user_id = validate_uuid(target_user_id, "userID")

    with transaction() as conn:
        with conn.cursor() as cur:
            current_role = get_project_role_with_cursor(
                cur,
                canonical_project_id,
                canonical_user_id,
            )
            require_role(current_role, {"owner"})
            existing_member = get_project_member_with_cursor(
                cur,
                canonical_project_id,
                canonical_target_user_id,
            )

            if existing_member is None:
                logger.info(
                    "Remove project member missed project_id=%s target_user_id=%s.",
                    canonical_project_id,
                    canonical_target_user_id,
                )
                raise ApiError(
                    type="user_not_found",
                    message="User is not a member of this project.",
                    status_code=404,
                )

            # Removing the last owner has the same effect as downgrading them:
            # no remaining user can manage project membership.
            if existing_member["role"] == "owner":
                ensure_not_last_owner(cur, canonical_project_id)

            cur.execute(
                queries.get("remove_project_member"),
                {
                    "project_id": canonical_project_id,
                    "user_id": canonical_target_user_id,
                },
            )

    logger.info(
        "Removed project member project_id=%s target_user_id=%s.",
        canonical_project_id,
        canonical_target_user_id,
    )
    return {"removed": True}


def get_project_role_with_cursor(cur: Any, project_id: str, user_id: str) -> str | None:
    """Return a user's project role using an existing DB cursor."""
    cur.execute(
        queries.get("get_project_member_role"),
        {
            "project_id": project_id,
            "user_id": user_id,
        },
    )
    row = cur.fetchone()
    return row["role"] if row else None


def get_project_member_with_cursor(cur: Any, project_id: str, user_id: str) -> Any:
    """Return project member details using an existing DB cursor."""
    cur.execute(
        queries.get("get_project_member"),
        {
            "project_id": project_id,
            "user_id": user_id,
        },
    )
    return cur.fetchone()


def get_project_for_user_with_cursor(cur: Any, project_id: str, user_id: str) -> Any:
    """Return project metadata plus the user's role using an existing DB cursor."""
    cur.execute(
        queries.get("get_project_for_user"),
        {
            "project_id": project_id,
            "user_id": user_id,
        },
    )
    return cur.fetchone()


def get_project_by_name_for_user_with_cursor(cur: Any, name: str, user_id: str) -> Any:
    """Return project metadata plus role by project name using an existing cursor."""
    cur.execute(
        queries.get("get_project_by_name_for_user"),
        {
            "name": name,
            "user_id": user_id,
        },
    )
    return cur.fetchone()


def require_role(role: str | None, allowed_roles: set[str]) -> None:
    """Require that a user has one of the allowed project roles.

    Missing membership is returned as project_not_found so callers cannot infer
    whether a project exists outside their access boundary.
    """
    if role is None:
        logger.info("Project role check failed because membership is missing.")
        raise project_not_found_error()

    if role not in allowed_roles:
        logger.info("Project role check forbidden role=%s allowed=%s.", role, sorted(allowed_roles))
        raise ApiError(
            type="forbidden",
            message="You do not have permission to perform this action.",
            status_code=403,
        )


def ensure_not_last_owner(cur: Any, project_id: str) -> None:
    """Prevent removing or downgrading the last project owner."""
    cur.execute(
        queries.get("count_project_owners"),
        {"project_id": project_id},
    )
    row = cur.fetchone()

    if row["owner_count"] <= 1:
        logger.info("Last owner protection rejected project_id=%s.", project_id)
        raise ApiError(
            type="validation_error",
            message="A project must have at least one owner.",
            status_code=400,
        )


def serialize_project(row: Any, *, role: str) -> dict[str, Any]:
    """Serialize a project row into API response shape."""
    return {
        "projectID": str(row["project_id"]),
        "name": row["name"],
        "slug": row["slug"],
        "k8s_namespace": row["k8s_namespace"],
        "created_at": to_iso8601(row["created_at"]),
        "role": role,
    }


def serialize_member(row: Any) -> dict[str, Any]:
    """Serialize a project member row into API response shape."""
    return {
        "userID": str(row["user_id"]),
        "email": row["email"],
        "role": row["role"],
        "created_at": to_iso8601(row["created_at"]),
    }


def project_not_found_error() -> ApiError:
    """Build a project-not-found response without leaking membership state."""
    return ApiError(
        type="project_not_found",
        message="Project not found.",
        status_code=404,
    )


def _insert_project_with_unique_slug(cur: Any, name: str, base_slug: str) -> Any:
    """Insert a project, retrying deterministic slug suffixes on collisions."""
    for suffix in _slug_suffixes():
        slug = _join_slug_suffix(base_slug, suffix)
        namespace = f"{Config.K8S_NAMESPACE_PREFIX}-{slug}"

        try:
            cur.execute(
                queries.get("create_project"),
                {
                    "name": name,
                    "slug": slug,
                    "k8s_namespace": namespace,
                },
            )
            return cur.fetchone()
        except Exception as exc:
            if not _is_unique_violation(exc):
                raise
            logger.info("Project slug collision slug=%s.", slug)

    raise ApiError(
        type="validation_error",
        message="Could not generate a unique project slug.",
        status_code=409,
    )


def _slug_suffixes() -> list[str]:
    """Return deterministic suffixes for resolving project slug collisions."""
    return [""] + [f"-{index}" for index in range(2, 100)]


def _join_slug_suffix(base_slug: str, suffix: str) -> str:
    """Append a suffix while preserving the 63-character DNS label limit."""
    max_base_length = 63 - len(suffix)
    return f"{base_slug[:max_base_length].strip('-')}{suffix}"


def _is_unique_violation(exc: Exception) -> bool:
    """Detect psycopg unique violations without importing psycopg globally."""
    return exc.__class__.__name__ == "UniqueViolation"
