"""Project business logic.

This service will create projects, generate unique slugs/namespaces, and manage
project-level authorization checks.
"""

from __future__ import annotations

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


queries = load_queries()
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
        raise project_not_found_error()

    return serialize_project(row, role=row["role"])


def delete_project(user_id: Any, project_id: Any) -> dict[str, bool]:
    """Delete a project when the current user is an owner.

    Kubernetes namespace cleanup will be added with the deployment lifecycle
    worker. For now, database cascades remove product metadata.
    """
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, {"owner"})
            cur.execute(
                queries.get("delete_project"),
                {"project_id": canonical_project_id},
            )
            row = cur.fetchone()

    if row is None:
        raise project_not_found_error()

    return {"deleted": True}


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
                    raise ApiError(
                        type="validation_error",
                        message="User is already a member of this project.",
                        status_code=409,
                    ) from exc
                raise

            membership = cur.fetchone()

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


def require_role(role: str | None, allowed_roles: set[str]) -> None:
    """Require that a user has one of the allowed project roles.

    Missing membership is returned as project_not_found so callers cannot infer
    whether a project exists outside their access boundary.
    """
    if role is None:
        raise project_not_found_error()

    if role not in allowed_roles:
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
    return exc.__class__.__name__ == "UniqueViolation"
