"""Project membership routes.

Project membership routes manage existing users inside a project and enforce
owner/member/viewer permissions.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.security.tokens import current_user_id, require_user_auth
from app.services import project_service
from app.utils.validation import require_field, require_json_object


bp = Blueprint("project_members", __name__, url_prefix="/v1/projects")


@bp.get("/<project_id>/members")
@require_user_auth
def list_project_members(project_id: str) -> tuple[object, int]:
    """List users who belong to a project."""
    # Any project member can view membership; role checks happen inside the
    # service using the authenticated user's project role.
    response = project_service.list_project_members(current_user_id(), project_id)
    return jsonify(response), 200


@bp.post("/<project_id>/members")
@require_user_auth
def add_project_member(project_id: str) -> tuple[object, int]:
    """Add an existing user to a project."""
    # There is no project_invites flow in the MVP, so adding a member requires
    # an existing user email and a concrete role.
    data = require_json_object(request.get_json(silent=True))
    response = project_service.add_project_member(
        user_id=current_user_id(),
        project_id=project_id,
        email=require_field(data, "email"),
        role=require_field(data, "role"),
    )
    return jsonify(response), 201


@bp.patch("/<project_id>/members/<target_user_id>")
@require_user_auth
def update_project_member_role(
    project_id: str,
    target_user_id: str,
) -> tuple[object, int]:
    """Update a project member's role."""
    # The service protects the last owner, so the route can stay focused on
    # extracting the target user and desired role from HTTP input.
    data = require_json_object(request.get_json(silent=True))
    response = project_service.update_project_member_role(
        user_id=current_user_id(),
        project_id=project_id,
        target_user_id=target_user_id,
        role=require_field(data, "role"),
    )
    return jsonify(response), 200


@bp.delete("/<project_id>/members/<target_user_id>")
@require_user_auth
def remove_project_member(project_id: str, target_user_id: str) -> tuple[object, int]:
    """Remove a project member."""
    # Removing members is owner-only and is checked in the service with the
    # current token user, not with any trusted client-side claim.
    response = project_service.remove_project_member(
        user_id=current_user_id(),
        project_id=project_id,
        target_user_id=target_user_id,
    )
    return jsonify(response), 200
