"""MiniTen Truss-compatible automation routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.security.tokens import get_bearer_token
from app.services import (
    account_api_key_service,
    model_deployment_service,
    project_service,
)
from app.utils.validation import require_field, require_json_object


bp = Blueprint("truss", __name__, url_prefix="/v1/truss")


def current_account_key_user_id() -> str:
    """Authenticate the request with an account API key and return user ID."""
    identity = account_api_key_service.authenticate_account_api_key(get_bearer_token())
    return identity["userID"]


@bp.post("/projects/init")
def init_project() -> tuple[object, int]:
    """Create-or-return a project for `truss init <name>`."""
    data = require_json_object(request.get_json(silent=True))
    project = project_service.create_project_if_missing(
        current_account_key_user_id(),
        require_field(data, "name"),
    )
    return jsonify({"project": project}), 200


@bp.post("/models")
def push_model() -> tuple[object, int]:
    """Deploy a model into the project named by the Truss directory."""
    data = require_json_object(request.get_json(silent=True))
    deployment = require_field(data, "deployment")
    if not isinstance(deployment, dict):
        from app.utils.errors import ApiError

        raise ApiError(
            type="validation_error",
            message="deployment must be a JSON object.",
            status_code=400,
        )
    response = model_deployment_service.create_model_deployment_for_account_project_name(
        current_account_key_user_id(),
        require_field(data, "project_name"),
        deployment,
    )
    return jsonify(response), 201


@bp.patch("/models")
def update_model() -> tuple[object, int]:
    """Update a model in the project named by the Truss directory."""
    data = require_json_object(request.get_json(silent=True))
    deployment = require_field(data, "deployment")
    if not isinstance(deployment, dict):
        from app.utils.errors import ApiError

        raise ApiError(
            type="validation_error",
            message="deployment must be a JSON object.",
            status_code=400,
        )
    response = model_deployment_service.update_model_deployment_for_account_project_name(
        current_account_key_user_id(),
        require_field(data, "project_name"),
        deployment,
    )
    return jsonify(response), 202
