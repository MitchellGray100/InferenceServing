"""Project API key routes.

API keys authenticate inference traffic later in the project. These
control-plane routes let authenticated users create, list, and revoke keys for
projects they belong to.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.security.tokens import current_user_id, require_user_auth
from app.services import api_key_service
from app.utils.validation import require_field, require_json_object


bp = Blueprint("api_keys", __name__, url_prefix="/v1/projects")


@bp.post("/<project_id>/api-keys")
@require_user_auth
def create_api_key(project_id: str) -> tuple[object, int]:
    """Create a project API key and return the raw key once."""
    # Only the create response includes the raw credential. Later list calls
    # return metadata only because the service stores only a keyed hash.
    data = require_json_object(request.get_json(silent=True))
    response = api_key_service.create_api_key(
        user_id=current_user_id(),
        project_id=project_id,
        name=require_field(data, "name"),
    )
    return jsonify(response), 201


@bp.get("/<project_id>/api-keys")
@require_user_auth
def list_api_keys(project_id: str) -> tuple[object, int]:
    """List project API key metadata without key hashes."""
    # This endpoint is safe for project viewers because it never returns the raw
    # key or stored HMAC.
    response = api_key_service.list_api_keys(current_user_id(), project_id)
    return jsonify(response), 200


@bp.delete("/<project_id>/api-keys/<api_key_id>")
@require_user_auth
def revoke_api_key(project_id: str, api_key_id: str) -> tuple[object, int]:
    """Revoke a project API key."""
    # Revocation is scoped by project ID and API key ID so one project's keys
    # cannot be affected through another project route.
    response = api_key_service.revoke_api_key(
        user_id=current_user_id(),
        project_id=project_id,
        api_key_id=api_key_id,
    )
    return jsonify(response), 200
