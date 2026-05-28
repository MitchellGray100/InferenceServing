"""Model deployment lifecycle business logic.

This service owns control-plane state for model deployments. Request handlers
call into it to validate project permissions, write deployment metadata, and
enqueue `deployment_jobs`; Kubernetes work is intentionally left for the worker.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import current_app

from app.db.pool import transaction
from app.db.sql import load_queries
from app.k8s import client as k8s_client
from app.k8s import deployment_manager
from app.k8s.names import build_model_resource_names
from app.services.project_service import (
    VIEW_ROLES,
    WRITE_ROLES,
    get_project_role_with_cursor,
    require_role,
)
from app.utils.errors import ApiError
from app.utils.time import to_iso8601
from app.utils.validation import (
    validate_deployment_name,
    validate_positive_int,
    validate_string,
    validate_uuid,
)


# Query files are loaded once per process and reused by service calls. Keeping
# SQL in `app/db/queries` makes transaction boundaries obvious in Python while
# still leaving the actual table operations easy to review.
queries = load_queries()
logger = logging.getLogger(__name__)

# These values are the exact enum strings allowed by the `deployment_jobs`
# schema. Route/service code uses the short action names for readability, then
# persists the database-facing job type string.
JOB_TYPES = {
    "deploy": "deploy_model",
    "start": "start_model",
    "stop": "stop_model",
    "scale": "scale_model",
    "delete": "delete_model",
}
# MVP defaults are intentionally modest so local kind/minikube clusters can run
# the control plane without requiring GPU hardware or large worker nodes. Real
# deployment sizing can be tightened once the Kubernetes worker is implemented.
DEFAULT_CPU_REQUEST = "1"
DEFAULT_CPU_LIMIT = "2"
DEFAULT_MEMORY_REQUEST = "4Gi"
DEFAULT_MEMORY_LIMIT = "8Gi"
MAX_MODEL_ID_LENGTH = 255
MAX_RESOURCE_TEXT_LENGTH = 32
MAX_VLLM_DTYPE_LENGTH = 32
MAX_REPLICAS = 100
MAX_GPU_COUNT = 16
MAX_MODEL_LEN = 262_144
DEFAULT_LOG_TAIL_LINES = 200
MAX_LOG_TAIL_LINES = 1000


def create_model_deployment(
    user_id: Any,
    project_id: Any,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Create deployment metadata and enqueue an initial deploy job.

    The model deployment row and the deployment job are inserted in the same
    transaction. That ensures the worker never sees a job for a missing
    deployment, and the API never returns a deployment that cannot be picked up
    by the worker.
    """
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    spec = validate_deployment_spec(data)

    with transaction() as conn:
        with conn.cursor() as cur:
            project = get_project_for_user_with_cursor(
                cur,
                canonical_project_id,
                canonical_user_id,
            )
            require_role(project["role"] if project else None, WRITE_ROLES)

            try:
                # The request handler records the desired state only. The
                # deployment worker will later create the namespace, PVC,
                # Deployment, Service, HPA, and optional Secret in Kubernetes.
                cur.execute(
                    queries.get("create_model_deployment"),
                    {
                        **spec,
                        **build_k8s_names(project["k8s_namespace"], spec["name"]),
                        "project_id": canonical_project_id,
                        "status": "deploying",
                        "created_by_user_id": canonical_user_id,
                    },
                )
            except Exception as exc:
                if _is_unique_violation(exc):
                    logger.info(
                        "Model deployment creation rejected duplicate name project_id=%s name=%s.",
                        canonical_project_id,
                        spec["name"],
                    )
                    raise ApiError(
                        type="validation_error",
                        message="A model deployment with that name already exists.",
                        status_code=409,
                    ) from exc
                raise

            deployment = cur.fetchone()
            job = enqueue_deployment_job_with_cursor(
                cur,
                canonical_project_id,
                deployment["model_deployment_id"],
                JOB_TYPES["deploy"],
                build_job_payload("deploy_model", deployment),
            )

    logger.info(
        "Created model deployment model_deployment_id=%s project_id=%s job_id=%s.",
        deployment["model_deployment_id"],
        canonical_project_id,
        job["deployment_job_id"],
    )
    return {
        "modelDeployment": serialize_model_deployment(deployment),
        "deploymentJob": serialize_deployment_job(job),
    }


def list_model_deployments(user_id: Any, project_id: Any) -> dict[str, list[dict[str, Any]]]:
    """List model deployments for any project member."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, VIEW_ROLES)
            cur.execute(
                queries.get("list_model_deployments"),
                {"project_id": canonical_project_id},
            )
            rows = cur.fetchall()

    logger.debug(
        "Listed model deployments project_id=%s count=%s.",
        canonical_project_id,
        len(rows),
    )
    return {"modelDeployments": [serialize_model_deployment(row) for row in rows]}


def get_model_deployment(
    user_id: Any,
    project_id: Any,
    model_deployment_id: Any,
) -> dict[str, Any]:
    """Return one model deployment if the current user can view the project."""
    deployment = get_model_deployment_for_user(
        user_id,
        project_id,
        model_deployment_id,
        VIEW_ROLES,
    )
    logger.debug(
        "Fetched model deployment model_deployment_id=%s project_id=%s.",
        model_deployment_id,
        project_id,
    )
    return serialize_model_deployment(deployment)


def list_model_deployment_jobs(
    user_id: Any,
    project_id: Any,
    model_deployment_id: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Return deployment command history for one model deployment.

    Unlike `get_model_deployment`, this endpoint intentionally allows
    soft-deleted deployments. The delete command is stored in
    `deployment_jobs`, so a dashboard or smoke test needs to read the final
    delete job even after the worker has marked the deployment deleted.
    """
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_model_deployment_id = validate_uuid(
        model_deployment_id,
        "modelDeploymentID",
    )

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, VIEW_ROLES)
            deployment = get_model_deployment_with_cursor(
                cur,
                canonical_project_id,
                canonical_model_deployment_id,
                include_deleted=True,
            )
            if deployment is None:
                logger.info(
                    "Deployment job history lookup missed model_deployment_id=%s project_id=%s.",
                    canonical_model_deployment_id,
                    canonical_project_id,
                )
                raise model_deployment_not_found_error()

            cur.execute(
                queries.get("list_deployment_jobs_for_model"),
                {"model_deployment_id": deployment["model_deployment_id"]},
            )
            rows = cur.fetchall()

    logger.debug(
        "Listed deployment jobs model_deployment_id=%s count=%s.",
        canonical_model_deployment_id,
        len(rows),
    )
    return {"deploymentJobs": [serialize_deployment_job(row) for row in rows]}


def list_model_logs(
    user_id: Any,
    project_id: Any,
    model_name: Any,
    *,
    tail: Any = None,
) -> dict[str, Any]:
    """Return recent Kubernetes pod logs for one named model deployment."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_model_name = validate_deployment_name(model_name)
    tail_lines = parse_log_tail(tail)

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, VIEW_ROLES)
            cur.execute(
                queries.get("get_model_deployment_by_name"),
                {
                    "project_id": canonical_project_id,
                    "name": canonical_model_name,
                },
            )
            deployment = cur.fetchone()

    if deployment is None:
        raise model_deployment_not_found_error()

    clients = k8s_client.create_clients()
    logs = deployment_manager.read_model_logs(
        clients,
        deployment,
        tail_lines=tail_lines,
    )
    logger.info(
        "Read Kubernetes logs project_id=%s model=%s pods=%s tail=%s.",
        canonical_project_id,
        canonical_model_name,
        len(logs),
        tail_lines,
    )
    return {
        "model": serialize_model_deployment(deployment),
        "logs": logs,
    }


def start_model_deployment(
    user_id: Any,
    project_id: Any,
    model_deployment_id: Any,
) -> dict[str, Any]:
    """Enqueue work to start a stopped or failed deployment.

    We use `deploying` as the interim API status because the database schema
    does not currently include a separate `starting` state. The worker can move
    the row to `loading`, `running`, or `failed` when Kubernetes state changes.
    """
    return lifecycle_command(
        user_id,
        project_id,
        model_deployment_id,
        job_type=JOB_TYPES["start"],
        requested_status="deploying",
        payload_extra={},
    )


def stop_model_deployment(
    user_id: Any,
    project_id: Any,
    model_deployment_id: Any,
) -> dict[str, Any]:
    """Enqueue work to stop a deployment.

    The API writes the desired stopped state immediately and leaves the actual
    replica/HPA changes to the worker. This keeps requests quick and preserves a
    durable command history in `deployment_jobs`.
    """
    return lifecycle_command(
        user_id,
        project_id,
        model_deployment_id,
        job_type=JOB_TYPES["stop"],
        requested_status="stopped",
        payload_extra={},
    )


def scale_model_deployment(
    user_id: Any,
    project_id: Any,
    model_deployment_id: Any,
    replicas: Any,
) -> dict[str, Any]:
    """Update desired replicas and enqueue scale work.

    `replicas = 0` is allowed so stop-like behavior can be represented as a
    scale request if the UI or CLI needs that later. The stop endpoint still
    exists because autoscaling-aware shutdown may require worker-specific logic.
    """
    desired_replicas = validate_positive_int(
        replicas,
        "replicas",
        min_value=0,
        max_value=MAX_REPLICAS,
    )
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_model_deployment_id = validate_uuid(model_deployment_id, "modelDeploymentID")

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, WRITE_ROLES)
            deployment = get_model_deployment_with_cursor(
                cur,
                canonical_project_id,
                canonical_model_deployment_id,
            )

            if deployment is None:
                logger.info(
                    "Scale model deployment missed model_deployment_id=%s project_id=%s.",
                    canonical_model_deployment_id,
                    canonical_project_id,
                )
                raise model_deployment_not_found_error()

            cur.execute(
                queries.get("advance_model_deployment_replicas"),
                {
                    "model_deployment_id": canonical_model_deployment_id,
                    "replicas": desired_replicas,
                },
            )
            deployment = cur.fetchone()
            job = enqueue_deployment_job_with_cursor(
                cur,
                canonical_project_id,
                canonical_model_deployment_id,
                JOB_TYPES["scale"],
                build_job_payload(
                    "scale_model",
                    deployment,
                    {"replicas": desired_replicas},
                ),
            )

    logger.info(
        "Enqueued scale model_deployment_id=%s project_id=%s replicas=%s job_id=%s.",
        canonical_model_deployment_id,
        canonical_project_id,
        desired_replicas,
        job["deployment_job_id"],
    )
    return {
        "modelDeployment": serialize_model_deployment(deployment),
        "deploymentJob": serialize_deployment_job(job),
    }


def delete_model_deployment(
    user_id: Any,
    project_id: Any,
    model_deployment_id: Any,
) -> dict[str, Any]:
    """Mark a deployment as deleting and enqueue delete work.

    The row is not hard-deleted here. The worker needs the metadata to locate
    Kubernetes resources, and the database keeps command history through
    `deployment_jobs`.
    """
    return lifecycle_command(
        user_id,
        project_id,
        model_deployment_id,
        job_type=JOB_TYPES["delete"],
        requested_status="deleting",
        payload_extra={},
    )


def lifecycle_command(
    user_id: Any,
    project_id: Any,
    model_deployment_id: Any,
    *,
    job_type: str,
    requested_status: str,
    payload_extra: dict[str, Any],
) -> dict[str, Any]:
    """Apply a simple status transition and enqueue a lifecycle job.

    Start, stop, and delete all follow the same pattern: authorize against the
    project, verify the deployment still exists, update the desired status, then
    enqueue a durable job in the same transaction.
    """
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_model_deployment_id = validate_uuid(model_deployment_id, "modelDeploymentID")

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, WRITE_ROLES)
            deployment = get_model_deployment_with_cursor(
                cur,
                canonical_project_id,
                canonical_model_deployment_id,
            )

            if deployment is None:
                logger.info(
                    "Lifecycle command missed model_deployment_id=%s project_id=%s job_type=%s.",
                    canonical_model_deployment_id,
                    canonical_project_id,
                    job_type,
                )
                raise model_deployment_not_found_error()

            cur.execute(
                queries.get("advance_model_deployment_status"),
                {
                    "model_deployment_id": canonical_model_deployment_id,
                    "status": requested_status,
                },
            )
            deployment = cur.fetchone()
            job = enqueue_deployment_job_with_cursor(
                cur,
                canonical_project_id,
                canonical_model_deployment_id,
                job_type,
                build_job_payload(job_type, deployment, payload_extra),
            )

    logger.info(
        "Enqueued model lifecycle command model_deployment_id=%s project_id=%s job_type=%s status=%s job_id=%s.",
        canonical_model_deployment_id,
        canonical_project_id,
        job_type,
        requested_status,
        job["deployment_job_id"],
    )
    return {
        "modelDeployment": serialize_model_deployment(deployment),
        "deploymentJob": serialize_deployment_job(job),
    }


def validate_deployment_spec(data: dict[str, Any]) -> dict[str, Any]:
    """Validate user-facing deployment creation input and apply MVP defaults.

    This intentionally accepts only the subset of vLLM/Kubernetes settings the
    MVP persists today. More detailed image allowlists, model-size policies, and
    quota enforcement can be layered on this function without changing route
    handlers.
    """
    if not isinstance(data, dict):
        raise ApiError(
            type="validation_error",
            message="Request body must be a JSON object.",
            status_code=400,
        )

    resources = _optional_dict(data.get("resources"), "resources")
    vllm = _optional_dict(data.get("vllm"), "vllm")
    autoscaling = _optional_dict(data.get("autoscaling"), "autoscaling")

    # Autoscaling fields are stored only when autoscaling is enabled. This keeps
    # non-autoscaled deployments simple: the worker reads `replicas` as the
    # desired fixed replica count and ignores HPA fields.
    autoscaling_enabled = bool(autoscaling.get("enabled", False))
    replicas = validate_positive_int(
        data.get("replicas", current_app.config["DEFAULT_MODEL_REPLICAS"]),
        "replicas",
        max_value=MAX_REPLICAS,
    )
    min_replicas = _optional_positive_int(
        autoscaling.get("min_replicas"),
        "min_replicas",
        default=current_app.config["DEFAULT_HPA_MIN_REPLICAS"]
        if autoscaling_enabled
        else None,
    )
    max_replicas = _optional_positive_int(
        autoscaling.get("max_replicas"),
        "max_replicas",
        default=current_app.config["DEFAULT_HPA_MAX_REPLICAS"]
        if autoscaling_enabled
        else None,
    )

    if autoscaling_enabled and min_replicas and max_replicas and min_replicas > max_replicas:
        raise ApiError(
            type="validation_error",
            message="min_replicas must be less than or equal to max_replicas.",
            status_code=400,
        )

    model_id = validate_string(
        data.get("model_id"),
        "model_id",
        max_length=MAX_MODEL_ID_LENGTH,
    )
    gpu_count = validate_positive_int(
        resources.get("gpu_count", 0),
        "gpu_count",
        min_value=0,
        max_value=MAX_GPU_COUNT,
    )

    if "image" in vllm:
        raise ApiError(
            type="validation_error",
            message="vllm.image is managed by MiniTen and cannot be set by clients.",
            status_code=400,
        )

    return {
        "name": validate_deployment_name(data.get("name")),
        "model_id": model_id,
        "replicas": replicas,
        "cpu_request": _optional_resource_text(
            resources.get("cpu_request"),
            "cpu_request",
            DEFAULT_CPU_REQUEST,
        ),
        "cpu_limit": _optional_resource_text(
            resources.get("cpu_limit"),
            "cpu_limit",
            DEFAULT_CPU_LIMIT,
        ),
        "memory_request": _optional_resource_text(
            resources.get("memory_request"),
            "memory_request",
            DEFAULT_MEMORY_REQUEST,
        ),
        "memory_limit": _optional_resource_text(
            resources.get("memory_limit"),
            "memory_limit",
            DEFAULT_MEMORY_LIMIT,
        ),
        "gpu_count": gpu_count,
        "vllm_image": select_vllm_image(model_id, gpu_count),
        "vllm_dtype": validate_string(
            vllm.get("dtype", "auto"),
            "dtype",
            max_length=MAX_VLLM_DTYPE_LENGTH,
        ),
        "vllm_max_model_len": validate_positive_int(
            vllm.get("max_model_len", 4096),
            "max_model_len",
            max_value=MAX_MODEL_LEN,
        ),
        "autoscaling_enabled": autoscaling_enabled,
        "min_replicas": min_replicas,
        "max_replicas": max_replicas,
        "target_cpu_utilization": _optional_positive_int(
            autoscaling.get("target_cpu_utilization"),
            "target_cpu_utilization",
            default=current_app.config["DEFAULT_HPA_TARGET_CPU_UTILIZATION"]
            if autoscaling_enabled
            else None,
            max_value=100,
        ),
    }


def build_k8s_names(k8s_namespace: str, deployment_name: str) -> dict[str, str]:
    """Build Kubernetes resource names for the MVP's fixed `v1` generation.

    The public model name remains project-local and stable. The internal
    Deployment/HPA names include `-v1` so a future rollout scheme has room to
    create a second Deployment while keeping the Service name stable.
    """
    names = build_model_resource_names(k8s_namespace, deployment_name)
    return {
        "k8s_namespace": names["k8s_namespace"],
        "k8s_deployment_name": names["k8s_deployment_name"],
        "k8s_service_name": names["k8s_service_name"],
        "k8s_hpa_name": names["k8s_hpa_name"],
    }


def select_vllm_image(model_id: str, gpu_count: int) -> str:
    """Choose MiniTen-managed vLLM image for a deployment.

    Clients do not provide container images. MiniTen owns that provisioning
    detail so CPU-only deployments get a CPU-capable image and GPU deployments
    get the standard GPU image. The local smoke model is a debug-only escape
    hatch so fast Kubernetes tests can use a tiny OpenAI-compatible container
    without exposing custom images in the public API.
    """
    if (
        current_app.config.get("API_DEBUG", False)
        and model_id == current_app.config.get("K8S_SMOKE_TEST_MODEL_ID")
    ):
        return current_app.config["K8S_SMOKE_TEST_IMAGE"]
    if gpu_count == 0:
        return current_app.config["VLLM_CPU_IMAGE"]
    return current_app.config["VLLM_IMAGE"]


def get_project_for_user_with_cursor(cur: Any, project_id: str, user_id: str) -> Any:
    """Return project metadata plus the user's role using an existing cursor."""
    cur.execute(
        queries.get("get_project_for_user"),
        {
            "project_id": project_id,
            "user_id": user_id,
        },
    )
    return cur.fetchone()


def get_model_deployment_for_user(
    user_id: Any,
    project_id: Any,
    model_deployment_id: Any,
    allowed_roles: set[str],
) -> Any:
    """Fetch a deployment after checking project membership."""
    canonical_user_id = validate_uuid(user_id, "userID")
    canonical_project_id = validate_uuid(project_id, "projectID")
    canonical_model_deployment_id = validate_uuid(model_deployment_id, "modelDeploymentID")

    with transaction() as conn:
        with conn.cursor() as cur:
            role = get_project_role_with_cursor(cur, canonical_project_id, canonical_user_id)
            require_role(role, allowed_roles)
            deployment = get_model_deployment_with_cursor(
                cur,
                canonical_project_id,
                canonical_model_deployment_id,
            )

    if deployment is None:
        logger.info(
            "Model deployment lookup missed model_deployment_id=%s project_id=%s.",
            canonical_model_deployment_id,
            canonical_project_id,
        )
        raise model_deployment_not_found_error()

    return deployment


def get_model_deployment_with_cursor(
    cur: Any,
    project_id: str,
    model_deployment_id: str,
    *,
    include_deleted: bool = False,
) -> Any:
    """Return one deployment using an existing DB cursor.

    Most callers use the non-deleted lookup because normal control-plane
    commands should not act on deleted models. Job history opts into
    `include_deleted` so completed delete jobs remain visible.
    """
    query_name = (
        "get_model_deployment_by_id_including_deleted"
        if include_deleted
        else "get_model_deployment_by_id"
    )
    cur.execute(
        queries.get(query_name),
        {
            "project_id": project_id,
            "model_deployment_id": model_deployment_id,
        },
    )
    return cur.fetchone()


def parse_log_tail(value: Any) -> int:
    """Parse the optional model log tail query parameter."""
    if value is None or value == "":
        return DEFAULT_LOG_TAIL_LINES
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(
            type="validation_error",
            message="tail must be an integer.",
            status_code=400,
            details={"field": "tail"},
        ) from exc
    return validate_positive_int(
        parsed,
        "tail",
        min_value=1,
        max_value=MAX_LOG_TAIL_LINES,
    )


def enqueue_deployment_job_with_cursor(
    cur: Any,
    project_id: str,
    model_deployment_id: Any,
    job_type: str,
    payload: dict[str, Any],
) -> Any:
    """Insert a deployment job inside the caller's transaction.

    `psycopg` is imported lazily here so unit tests that only exercise route
    wiring and validation do not need a database driver at import time.
    """
    from psycopg.types.json import Jsonb

    cur.execute(
        queries.get("create_deployment_job"),
        {
            "project_id": project_id,
            "model_deployment_id": model_deployment_id,
            "job_type": job_type,
            "desired_generation": payload["desired_generation"],
            "payload": Jsonb(payload),
        },
    )
    job = cur.fetchone()
    logger.debug(
        "Inserted deployment job job_id=%s model_deployment_id=%s job_type=%s generation=%s.",
        job["deployment_job_id"],
        model_deployment_id,
        job_type,
        payload["desired_generation"],
    )
    return job


def build_job_payload(
    job_type: str,
    deployment: Any,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the durable payload consumed later by the deployment worker.

    The payload duplicates the resource names from `model_deployments` on
    purpose. Jobs are historical records of what was requested, so they should
    remain understandable even if deployment metadata changes later.
    """
    payload = {
        "job_type": job_type,
        "project_id": str(deployment["project_id"]),
        "model_deployment_id": str(deployment["model_deployment_id"]),
        "name": deployment["name"],
        "model_id": deployment["model_id"],
        "k8s_namespace": deployment["k8s_namespace"],
        "k8s_deployment_name": deployment["k8s_deployment_name"],
        "k8s_service_name": deployment["k8s_service_name"],
        "k8s_hpa_name": deployment["k8s_hpa_name"],
        "replicas": deployment["replicas"],
        "desired_generation": deployment.get("desired_generation", 1),
        "autoscaling_enabled": deployment["autoscaling_enabled"],
    }

    if extra:
        payload.update(extra)

    return payload


def serialize_model_deployment(row: Any) -> dict[str, Any]:
    """Serialize a model deployment row into API response shape."""
    return {
        "modelDeploymentID": str(row["model_deployment_id"]),
        "projectID": str(row["project_id"]),
        "name": row["name"],
        "model_id": row["model_id"],
        "status": row["status"],
        "k8s_namespace": row["k8s_namespace"],
        "k8s_deployment_name": row["k8s_deployment_name"],
        "k8s_service_name": row["k8s_service_name"],
        "k8s_hpa_name": row["k8s_hpa_name"],
        "replicas": row["replicas"],
        "desired_generation": row.get("desired_generation", 1),
        "resources": {
            "cpu_request": row["cpu_request"],
            "cpu_limit": row["cpu_limit"],
            "memory_request": row["memory_request"],
            "memory_limit": row["memory_limit"],
            "gpu_count": row["gpu_count"],
        },
        "vllm": {
            "image": row["vllm_image"],
            "dtype": row["vllm_dtype"],
            "max_model_len": row["vllm_max_model_len"],
        },
        "autoscaling": {
            "enabled": row["autoscaling_enabled"],
            "min_replicas": row["min_replicas"],
            "max_replicas": row["max_replicas"],
            "target_cpu_utilization": row["target_cpu_utilization"],
        },
        "created_at": to_iso8601(row["created_at"]),
        "updated_at": to_iso8601(row["updated_at"]),
        "deleted_at": _optional_iso8601(row["deleted_at"]),
    }


def serialize_deployment_job(row: Any) -> dict[str, Any]:
    """Serialize a deployment job row for command responses."""
    return {
        "deploymentJobID": str(row["deployment_job_id"]),
        "projectID": str(row["project_id"]),
        "modelDeploymentID": str(row["model_deployment_id"])
        if row["model_deployment_id"] is not None
        else None,
        "job_type": row["job_type"],
        "desired_generation": row.get("desired_generation", 1),
        "status": row["status"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "last_error": row["last_error"],
        "created_at": to_iso8601(row["created_at"]),
        "updated_at": to_iso8601(row["updated_at"]),
    }


def model_deployment_not_found_error() -> ApiError:
    """Build the standard model deployment not found response."""
    return ApiError(
        type="model_deployment_not_found",
        message="Model deployment not found.",
        status_code=404,
    )


def _optional_dict(value: Any, field: str) -> dict[str, Any]:
    """Return an optional JSON object field, rejecting arrays/scalars."""
    if value is None:
        return {}

    if not isinstance(value, dict):
        raise ApiError(
            type="validation_error",
            message=f"{field} must be a JSON object.",
            status_code=400,
        )

    return value


def _optional_resource_text(value: Any, field: str, default: str) -> str:
    """Validate a resource quantity-like string while applying a default."""
    return validate_string(
        value if value is not None else default,
        field,
        max_length=MAX_RESOURCE_TEXT_LENGTH,
    )


def _optional_positive_int(
    value: Any,
    field: str,
    *,
    default: int | None = None,
    max_value: int | None = None,
) -> int | None:
    """Validate an optional integer and preserve `None` when no default applies."""
    if value is None:
        return default

    return validate_positive_int(value, field, max_value=max_value)


def _optional_iso8601(value: Any) -> str | None:
    """Serialize optional timestamps in response payloads."""
    return to_iso8601(value) if value is not None else None


def _is_unique_violation(exc: Exception) -> bool:
    """Detect psycopg unique violations without importing psycopg globally."""
    return exc.__class__.__name__ == "UniqueViolation"
