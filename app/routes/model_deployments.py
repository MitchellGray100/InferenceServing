"""Model deployment lifecycle routes.

These endpoints create deployment metadata and enqueue `deployment_jobs`. They
do not call Kubernetes directly from request handlers.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.security.tokens import current_user_id, require_user_auth
from app.services import idempotency_service, model_deployment_service
from app.utils.validation import require_field, require_json_object


# Model deployment routes are nested under projects because project membership
# is the authorization boundary for every model lifecycle operation.
bp = Blueprint("model_deployments", __name__, url_prefix="/v1/projects")


@bp.post("/<project_id>/models")
@require_user_auth
def create_model_deployment(project_id: str) -> tuple[object, int]:
    """Create model deployment metadata and enqueue deploy work."""
    data = require_json_object(request.get_json(silent=True))
    user_id = current_user_id()
    response, status = idempotency_service.run_idempotent_control_plane_request(
        project_id=project_id,
        user_id=user_id,
        idempotency_key=request.headers.get("Idempotency-Key"),
        method=request.method,
        path=request.path,
        body=data,
        action=lambda: (
            model_deployment_service.create_model_deployment(
                user_id=user_id,
                project_id=project_id,
                data=data,
            ),
            201,
        ),
    )
    return jsonify(response), status


@bp.get("/<project_id>/models")
@require_user_auth
def list_model_deployments(project_id: str) -> tuple[object, int]:
    """List model deployments for a project."""
    response = model_deployment_service.list_model_deployments(
        current_user_id(),
        project_id,
    )
    return jsonify(response), 200


@bp.get("/<project_id>/models/<model_deployment_id>")
@require_user_auth
def get_model_deployment(
    project_id: str,
    model_deployment_id: str,
) -> tuple[object, int]:
    """Return one model deployment."""
    response = model_deployment_service.get_model_deployment(
        current_user_id(),
        project_id,
        model_deployment_id,
    )
    return jsonify(response), 200


@bp.patch("/<project_id>/models/<model_deployment_id>")
@require_user_auth
def update_model_deployment_settings(
    project_id: str,
    model_deployment_id: str,
) -> tuple[object, int]:
    """Update deployment settings and enqueue Kubernetes reapply work."""
    data = require_json_object(request.get_json(silent=True))
    user_id = current_user_id()
    response, status = idempotency_service.run_idempotent_control_plane_request(
        project_id=project_id,
        user_id=user_id,
        idempotency_key=request.headers.get("Idempotency-Key"),
        method=request.method,
        path=request.path,
        body=data,
        action=lambda: (
            model_deployment_service.update_model_deployment_settings(
                user_id=user_id,
                project_id=project_id,
                model_deployment_id=model_deployment_id,
                data=data,
            ),
            202,
        ),
    )
    return jsonify(response), status


@bp.get("/<project_id>/models/<model_deployment_id>/jobs")
@require_user_auth
def list_model_deployment_jobs(
    project_id: str,
    model_deployment_id: str,
) -> tuple[object, int]:
    """Return deployment command history for one model deployment."""
    response = model_deployment_service.list_model_deployment_jobs(
        current_user_id(),
        project_id,
        model_deployment_id,
    )
    return jsonify(response), 200


@bp.get("/<project_id>/models/<model_deployment_id>/status")
@require_user_auth
def get_model_deployment_status(
    project_id: str,
    model_deployment_id: str,
) -> tuple[object, int]:
    """Return DB, job, and live Kubernetes status for one deployment."""
    response = model_deployment_service.get_model_deployment_status(
        current_user_id(),
        project_id,
        model_deployment_id,
    )
    return jsonify(response), 200


@bp.get("/<project_id>/models/<model_name>/logs")
@require_user_auth
def list_model_logs(project_id: str, model_name: str) -> tuple[object, int]:
    """Return recent Kubernetes pod logs for one project-local model name."""
    response = model_deployment_service.list_model_logs(
        current_user_id(),
        project_id,
        model_name,
        tail=request.args.get("tail"),
    )
    return jsonify(response), 200


@bp.post("/<project_id>/models/<model_deployment_id>/start")
@require_user_auth
def start_model_deployment(
    project_id: str,
    model_deployment_id: str,
) -> tuple[object, int]:
    """Enqueue start work for a model deployment.

    Command endpoints return 202 because Kubernetes work happens asynchronously
    in the deployment worker after the job row is committed.
    """
    user_id = current_user_id()
    response, status = idempotency_service.run_idempotent_control_plane_request(
        project_id=project_id,
        user_id=user_id,
        idempotency_key=request.headers.get("Idempotency-Key"),
        method=request.method,
        path=request.path,
        body={},
        action=lambda: (
            model_deployment_service.start_model_deployment(
                user_id,
                project_id,
                model_deployment_id,
            ),
            202,
        ),
    )
    return jsonify(response), status


@bp.post("/<project_id>/models/<model_deployment_id>/stop")
@require_user_auth
def stop_model_deployment(
    project_id: str,
    model_deployment_id: str,
) -> tuple[object, int]:
    """Enqueue stop work for a model deployment.

    The API response confirms the command has been queued, not that Kubernetes
    has already scaled the deployment down.
    """
    user_id = current_user_id()
    response, status = idempotency_service.run_idempotent_control_plane_request(
        project_id=project_id,
        user_id=user_id,
        idempotency_key=request.headers.get("Idempotency-Key"),
        method=request.method,
        path=request.path,
        body={},
        action=lambda: (
            model_deployment_service.stop_model_deployment(
                user_id,
                project_id,
                model_deployment_id,
            ),
            202,
        ),
    )
    return jsonify(response), status


@bp.post("/<project_id>/models/<model_deployment_id>/hard-restart")
@require_user_auth
def hard_restart_model_deployment(
    project_id: str,
    model_deployment_id: str,
) -> tuple[object, int]:
    """Force-delete runtime resources and enqueue deployment recreation."""
    user_id = current_user_id()
    response, status = idempotency_service.run_idempotent_control_plane_request(
        project_id=project_id,
        user_id=user_id,
        idempotency_key=request.headers.get("Idempotency-Key"),
        method=request.method,
        path=request.path,
        body={},
        action=lambda: (
            model_deployment_service.hard_restart_model_deployment(
                user_id,
                project_id,
                model_deployment_id,
            ),
            202,
        ),
    )
    return jsonify(response), status


@bp.post("/<project_id>/models/<model_deployment_id>/scale")
@require_user_auth
def scale_model_deployment(
    project_id: str,
    model_deployment_id: str,
) -> tuple[object, int]:
    """Update desired replicas and enqueue scale work.

    The request body is deliberately narrow for the MVP: scaling is a distinct
    command with one required `replicas` field.
    """
    data = require_json_object(request.get_json(silent=True))
    user_id = current_user_id()
    response, status = idempotency_service.run_idempotent_control_plane_request(
        project_id=project_id,
        user_id=user_id,
        idempotency_key=request.headers.get("Idempotency-Key"),
        method=request.method,
        path=request.path,
        body=data,
        action=lambda: (
            model_deployment_service.scale_model_deployment(
                user_id=user_id,
                project_id=project_id,
                model_deployment_id=model_deployment_id,
                replicas=require_field(data, "replicas"),
            ),
            202,
        ),
    )
    return jsonify(response), status


@bp.post("/<project_id>/models/<model_deployment_id>/sync")
@require_user_auth
def sync_model_deployment_status(
    project_id: str,
    model_deployment_id: str,
) -> tuple[object, int]:
    """Enqueue status reconciliation from live Kubernetes state."""
    user_id = current_user_id()
    response, status = idempotency_service.run_idempotent_control_plane_request(
        project_id=project_id,
        user_id=user_id,
        idempotency_key=request.headers.get("Idempotency-Key"),
        method=request.method,
        path=request.path,
        body={},
        action=lambda: (
            model_deployment_service.sync_model_deployment_status(
                user_id,
                project_id,
                model_deployment_id,
            ),
            202,
        ),
    )
    return jsonify(response), status


@bp.delete("/<project_id>/models/<model_deployment_id>")
@require_user_auth
def delete_model_deployment(
    project_id: str,
    model_deployment_id: str,
) -> tuple[object, int]:
    """Mark a model deployment as deleting and enqueue delete work.

    Deletion is asynchronous so the worker can remove Kubernetes resources
    before the database row is finally marked deleted.
    """
    user_id = current_user_id()
    response, status = idempotency_service.run_idempotent_control_plane_request(
        project_id=project_id,
        user_id=user_id,
        idempotency_key=request.headers.get("Idempotency-Key"),
        method=request.method,
        path=request.path,
        body={},
        action=lambda: (
            model_deployment_service.delete_model_deployment(
                user_id,
                project_id,
                model_deployment_id,
            ),
            202,
        ),
    )
    return jsonify(response), status
