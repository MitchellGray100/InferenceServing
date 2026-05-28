"""Analytics and model event routes.

Analytics routes read lightweight request metadata and model lifecycle events.
They should not expose stored prompts or model responses in the MVP.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.security.tokens import current_user_id, require_user_auth
from app.services import analytics_service


# Analytics routes are nested under projects because project membership controls
# whether a user may inspect model traffic and deployment history.
bp = Blueprint("analytics", __name__, url_prefix="/v1/projects")


@bp.get("/<project_id>/analytics/overview")
@require_user_auth
def get_project_overview(project_id: str) -> tuple[object, int]:
    """Return project-level analytics summary across all models."""
    response = analytics_service.get_project_overview(
        user_id=current_user_id(),
        project_id=project_id,
    )
    return jsonify(response), 200


@bp.get("/<project_id>/analytics/models/<model_name>/metrics")
@require_user_auth
def get_model_metrics(project_id: str, model_name: str) -> tuple[object, int]:
    """Return aggregate request metrics for one project-local model name."""
    response = analytics_service.get_model_metrics(
        user_id=current_user_id(),
        project_id=project_id,
        model_name=model_name,
        since=request.args.get("since"),
    )
    return jsonify(response), 200


@bp.get("/<project_id>/analytics/models/<model_name>/requests")
@require_user_auth
def list_model_requests(project_id: str, model_name: str) -> tuple[object, int]:
    """Return recent request metadata for one project-local model name."""
    response = analytics_service.list_model_requests(
        user_id=current_user_id(),
        project_id=project_id,
        model_name=model_name,
        limit=request.args.get("limit"),
        status_code=request.args.get("status_code"),
        since=request.args.get("since"),
    )
    return jsonify(response), 200


@bp.get("/<project_id>/analytics/models/<model_name>/events")
@require_user_auth
def list_model_events(project_id: str, model_name: str) -> tuple[object, int]:
    """Return model lifecycle events for one project-local model name."""
    response = analytics_service.list_model_events(
        user_id=current_user_id(),
        project_id=project_id,
        model_name=model_name,
    )
    return jsonify(response), 200
