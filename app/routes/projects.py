"""Project routes.

Projects are the primary isolation boundary for members, API keys, model
deployments, inference logs, and Kubernetes namespaces.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.security.tokens import current_user_id, require_user_auth
from app.services import project_service
from app.utils.validation import require_field, require_json_object


bp = Blueprint("projects", __name__, url_prefix="/v1/projects")


@bp.post("")
@require_user_auth
def create_project() -> tuple[object, int]:
    """Create a project and owner membership for the current user."""
    data = require_json_object(request.get_json(silent=True))
    response = project_service.create_project(
        user_id=current_user_id(),
        name=require_field(data, "name"),
    )
    return jsonify(response), 201


@bp.get("")
@require_user_auth
def list_projects() -> tuple[object, int]:
    """List projects the current user belongs to."""
    return jsonify(project_service.list_projects(current_user_id())), 200


@bp.get("/<project_id>")
@require_user_auth
def get_project(project_id: str) -> tuple[object, int]:
    """Return one project if the current user is a member."""
    return jsonify(project_service.get_project(current_user_id(), project_id)), 200


@bp.delete("/<project_id>")
@require_user_auth
def delete_project(project_id: str) -> tuple[object, int]:
    """Delete a project if the current user is an owner."""
    return jsonify(project_service.delete_project(current_user_id(), project_id)), 200
